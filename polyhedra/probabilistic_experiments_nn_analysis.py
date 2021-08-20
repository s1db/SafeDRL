import csv
import datetime
import math
import os
import sys
import time
from collections import defaultdict
from contextlib import nullcontext
from typing import Tuple, List
import gurobipy as grb
import networkx
import numpy as np
import progressbar
import ray
import torch
from interval import interval, imath
import plotly.graph_objects as go
from py4j.java_gateway import JavaGateway
import pickle
from polyhedra.experiments_nn_analysis import Experiment
from polyhedra.partitioning import sample_and_split, pick_longest_dimension, split_polyhedron, is_split_range
from polyhedra.plot_utils import show_polygon_list3, show_polygon_list31d, show_polygons
from polyhedra.prism_methods import calculate_target_probabilities, recreate_prism_PPO, extract_probabilities
from utility.standard_progressbar import StandardProgressBar


class ProbabilisticExperiment(Experiment):
    def __init__(self, env_input_size: int):
        super().__init__(env_input_size)
        self.use_entropy_split = True
        self.use_split = True  # enable/disable splitting
        self.load_graph = False
        self.use_contained = False  # enable/disable containment check
        self.max_probability_split = 0.33
        self.save_graph = True

    def run_experiment(self):
        assert self.get_nn_fn is not None
        assert self.plot_fn is not None
        assert self.post_fn_remote is not None
        assert self.template_2d is not None
        assert self.input_template is not None
        assert self.input_boundaries is not None
        assert self.analysis_template is not None
        assert self.unsafe_zone is not None
        assert self.assign_lbl_fn is not None
        experiment_start_time = time.time()
        nn: torch.nn.Sequential = self.get_nn_fn()

        max_t, num_already_visited, vertices_list, unsafe = self.main_loop(nn, self.analysis_template, self.template_2d)
        print(f"T={max_t}")

        print(f"The algorithm skipped {num_already_visited} already visited states")
        safe = None
        if unsafe:
            print("The agent is unsafe")
            safe = False
        elif max_t < self.time_horizon:
            print("The agent is safe")
            safe = True
        else:
            print(f"It could not be determined if the agent is safe or not within {self.time_horizon} steps. Increase 'time_horizon' to increase the number of steps to analyse")
            safe = None
        experiment_end_time = time.time()
        elapsed_seconds = round((experiment_end_time - experiment_start_time))
        print(f"Total verification time {str(datetime.timedelta(seconds=elapsed_seconds))}")
        return elapsed_seconds, safe, max_t

    class LoopStats():
        """Class that contains some information to share inbetween loops"""

        def __init__(self):
            self.seen = []
            self.frontier = []  # contains the elements to explore
            self.to_replace_split = []  # elements to replace/split because they are too big
            self.vertices_list = defaultdict(list)  # contains point at every timesteps, this is for plotting purposes only
            self.max_t = 0
            self.num_already_visited = 0
            self.num_irrelevant = 0
            self.proc_ids = []
            self.last_time_plot = None
            self.exit_flag = False
            self.is_agent_unsafe = False
            self.root = None

    def main_loop(self, nn, template, template_2d):
        root = self.generate_root_polytope()
        root_pair = (root, 0)  # label for root is always 0
        root_list = [root_pair]
        if not self.load_graph:
            stats = ProbabilisticExperiment.LoopStats()
            stats.root = root_pair
            if self.additional_seen_fn is not None:
                for extra in self.additional_seen_fn():
                    stats.seen.append(extra)
            stats.frontier = [(0, x) for x in root_list]
            if self.graph is not None:
                self.graph.add_node(root_pair)
            widgets = [progressbar.Variable('n_workers'), ', ', progressbar.Variable('frontier'), ', ', progressbar.Variable('seen'), ', ', progressbar.Variable('num_already_visited'), ", ",
                       progressbar.Variable('max_t'), ", ", progressbar.Variable('last_visited_state')]
            if self.before_start_fn is not None:
                self.before_start_fn(nn)
            with progressbar.ProgressBar(widgets=widgets) if self.show_progressbar else nullcontext() as bar_main:
                while len(stats.frontier) != 0 or len(stats.proc_ids) != 0:
                    self.inner_loop_step(stats, template_2d, template, nn, bar_main)
            self.plot_fn(stats.vertices_list, template, template_2d)
            gateway, mc, mdp, mapping = recreate_prism_PPO(self.graph, root_pair)
            inv_map = {v: k for k, v in mapping.items()}
            networkx.write_gpickle(self.graph, os.path.join(self.save_dir, "graph.p"))
            pickle.dump(stats, open(os.path.join(self.save_dir, "stats.p"), "wb"))
        else:
            self.graph = networkx.read_gpickle(os.path.join(self.save_dir, "graph.p"))
            stats: ProbabilisticExperiment.LoopStats = pickle.load(open(os.path.join(self.save_dir, "stats.p"), "rb"))
            gateway, mc, mdp, mapping = recreate_prism_PPO(self.graph, root_pair)
        # ----for plotting
        colours = []
        # to_plot = list(self.graph.successors(root_pair))
        to_plot = list(self.graph.successors(list(self.graph.successors(root_pair))[0]))
        to_plot = stats.vertices_list[1]
        bad_nodes = []
        for x in self.graph.adj:
            safe = self.graph.nodes[x].get("safe")
            irrelevant = self.graph.nodes[x].get("irrelevant")
            if safe is not None:
                if safe is False:
                    bad_nodes.append(x)
            if irrelevant is not None:
                if irrelevant is True:
                    bad_nodes.append(x)
        terminal_states_ids = [mapping[x] for x in bad_nodes]
        for node in to_plot:
            starting_id = mapping[node]
            sum = 0
            # for terminal_state_id in terminal_states_ids:
            #     minmin, minmax, maxmin, maxmax = extract_probabilities([terminal_state_id], gateway, mc, mdp, root_id=starting_id)
            #     sum+=maxmax
            minmin, minmax, maxmin, maxmax = extract_probabilities(terminal_states_ids, gateway, mc, mdp, root_id=starting_id)
            colours.append(np.mean([maxmin, maxmax]))
        # for x,x_label in to_plot:
        #     ranges_probs1 = self.create_range_bounds_model(template, x, self.env_input_size, nn)
        #     colours.append(np.mean(ranges_probs1[0]))
        print("", file=sys.stderr)  # new line
        fig = show_polygons(template, [x[0] for x in to_plot], self.template_2d, colours)
        fig.write_html("initial_state_unsafe_prob.html")
        fig.show()
        print("", file=sys.stderr)  # new line
        return stats.max_t, stats.num_already_visited, stats.vertices_list, stats.is_agent_unsafe

    @ray.remote
    def post_milp(self, x, x_label, nn, output_flag, t, template):
        raise NotImplementedError()  # need to implement this method, remember to put @ray.remote as an attribute

    def inner_loop_step(self, stats: LoopStats, template_2d, template, nn, bar_main):
        # fills up the worker threads
        while len(stats.proc_ids) < self.n_workers and len(stats.frontier) != 0:
            t, (x, x_label) = stats.frontier.pop(0) if self.use_bfs else stats.frontier.pop()
            if t > self.time_horizon:
                print(f"Discard timestep t={t}")
                continue
            # if stats.max_t > self.time_horizon:
            #     print(f"Reached horizon t={t}")
            #     stats.exit_flag = True
            #     break
            just_split = False
            for (s, s_label) in stats.to_replace_split:
                if s_label == x_label:
                    if s == x:
                        just_split = True
                        break
            stats.max_t = max(stats.max_t, t)
            stats.vertices_list[t].append(np.array(x))
            stats.seen.append((x, x_label))

            if not just_split and self.use_contained:
                contained_flag = False
                to_remove = []
                for (s, s_label) in stats.seen:
                    if s_label == x_label:
                        if contained(x, s):
                            if not self.graph.has_predecessor((x, x_label), (s, x_label)):  # ensures that if there was a split it doesn't count as contained
                                self.graph.add_edge((x, x_label), (s, x_label), action="contained", lb=1.0, ub=1.0)
                                contained_flag = True
                                break
                        if contained(s, x):
                            to_remove.append((s, s_label))
                for rem in to_remove:
                    stats.num_already_visited += 1
                    stats.seen.remove(rem)
                if contained_flag:
                    stats.num_already_visited += 1
                    continue

            else:
                if just_split:
                    stats.vertices_list[t].remove(np.array(x))

            if just_split:
                self.graph.nodes[(x, x_label)]
                # todo find parent
                # todo find number of splits
                # todo replace element with the splitted ones
                new_frontier = []
                split1, split2 = sample_and_split(self.get_pre_nn(), nn, template, np.array(x), self.env_input_size, template_2d)
                successor_info = Experiment.SuccessorInfo()
                successor_info.successor = tuple(split1)
                successor_info.parent = x
                successor_info.parent_lbl = x_label
                successor_info.t = t
                successor_info.action = f"split{1}"
                new_frontier.append(successor_info)
                successor_info = Experiment.SuccessorInfo()
                successor_info.successor = tuple(split2)
                successor_info.parent = x
                successor_info.parent_lbl = x_label
                successor_info.t = t
                successor_info.action = f"split{2}"
                new_frontier.append(successor_info)
                stats.proc_ids.append(ray.put(new_frontier))

            else:
                split_proc_id = self.check_split(t, x, x_label, nn, bar_main, stats, template, template_2d)
                if split_proc_id is not None:  # splitting
                    stats.proc_ids.append(split_proc_id)
                else:  # calculate successor
                    stats.proc_ids.append(self.post_fn_remote.remote(self, x, x_label, nn, self.output_flag, t, template))

            if self.show_progressbar:
                bar_main.update(value=bar_main.value + 1, n_workers=len(stats.proc_ids), seen=len(stats.seen), frontier=len(stats.frontier), num_already_visited=stats.num_already_visited,
                                last_visited_state=str(x),
                                max_t=stats.max_t)

        if stats.last_time_plot is None or time.time() - stats.last_time_plot >= self.plotting_time_interval:
            if stats.last_time_plot is not None:
                self.plot_fn(stats.vertices_list, template, template_2d)
            stats.last_time_plot = time.time()
        if self.update_progress_fn is not None:
            self.update_progress_fn(n_workers=len(stats.proc_ids), seen=len(stats.seen), frontier=len(stats.frontier), num_already_visited=stats.num_already_visited, max_t=stats.max_t)

        # process the results (if any)
        new_frontier = self.collect_results(stats, template)
        # update prism
        self.update_prism_step(stats.frontier, new_frontier, stats.root, stats)
        # todo go through the tree and decide if we want to split already visited nodes based on the max and min probability of encountering a terminal state
        stats.new_frontier = []  # resets the new_frontier
        if self.save_graph:
            networkx.write_gpickle(self.graph, os.path.join(self.save_dir, "graph.p"))
            pickle.dump(stats, open(os.path.join(self.save_dir, "stats.p"), "wb"))

    def collect_results(self, stats, template):
        """collects the results from the various processes, creates a list with the newly added states"""
        new_frontier = []
        ready_ids, stats.proc_ids = ray.wait(stats.proc_ids, num_returns=len(stats.proc_ids), timeout=0.5)
        if len(ready_ids) != 0:
            results_list = ray.get(ready_ids)
            assert len(results_list) != 0, "something is wrong with the calculation of the successor"
            for results in results_list:
                successor_info: Experiment.SuccessorInfo
                for successor_info in results:  # these are the individual elements in the list which is returned by post_milp
                    successor_info.successor_lbl = self.assign_lbl_fn(successor_info.successor, successor_info.parent, successor_info.parent_lbl)
                    if self.use_rounding:
                        x_prime_rounded = self.round_tuple(successor_info.successor, self.rounding_value)

                        assert contained(successor_info.successor,
                                         x_prime_rounded), f"{successor_info.successor} not contained in {x_prime_rounded}"  # x_prime_rounded should always be bigger than x_prime
                        successor_info.successor = x_prime_rounded
                    # stats.frontier = [(u, (y, y_label)) for u, (y, y_label) in stats.frontier if
                    #                   not (y_label == successor_info.successor_lbl and contained(y, successor_info.successor))]  # remove from frontier if the successor is bigger
                    is_splitted = "split" in successor_info.action
                    # ---check contained
                    is_contained = False
                    contained_item = None
                    if not is_splitted:
                        is_contained, contained_item = find_contained(successor_info, stats.seen)
                    else:
                        if successor_info.get_parent_node() in stats.seen:
                            stats.seen.remove(successor_info.get_parent_node())  # remove the parent node from seen if it has been split to prevent unnecessary loops
                    if is_splitted or not is_contained:
                        unsafe = self.check_unsafe(template, successor_info.successor, self.unsafe_zone)
                        if self.graph is not None:
                            self.graph.add_edge(successor_info.get_parent_node(), successor_info.get_successor_node(), action=successor_info.action, lb=successor_info.lb, ub=successor_info.ub)
                        if not unsafe:
                            new_frontier.append((successor_info.t, successor_info.get_successor_node()))  # put the item back into the queue
                        else:  # unsafe
                            self.graph.nodes[successor_info.get_successor_node()]["safe"] = False
                            stats.seen.append(successor_info.get_successor_node())
                        # print(x_prime)
                    else:
                        stats.num_already_visited += 1
                        if self.graph is not None:
                            self.graph.add_edge(successor_info.get_parent_node(), contained_item, action=successor_info.action, lb=successor_info.lb,
                                                ub=successor_info.ub)  # todo double check this is how you want to contain
        return new_frontier

    def update_prism_step(self, frontier, new_frontier, root, stats):
        gateway, mc, mdp, mapping = recreate_prism_PPO(self.graph, root)
        inv_map = {v: k for k, v in mapping.items()}
        with StandardProgressBar(prefix="Updating probabilities ", max_value=len(new_frontier) + 1).start() as bar:
            for t, x in new_frontier:
                try:
                    minmin, minmax, maxmin, maxmax = calculate_target_probabilities(gateway, mc, mdp, mapping, targets=[x])
                    assert maxmax != 0
                    bar.update(bar.value + 1)
                except Exception as e:
                    print("warning")
                    print(e)
                if maxmax > 1e-6:  # check if state is irrelevant
                    frontier.append((t, x))  # next computation only considers relevant states
                else:
                    self.graph.nodes[x]["irrelevant"] = True
                    stats.num_irrelevant += 1

        bad_nodes = []
        for x in self.graph.adj:
            safe = self.graph.nodes[x].get("safe")
            irrelevant = self.graph.nodes[x].get("irrelevant")
            if safe is not None:
                if safe is False:
                    bad_nodes.append(x)
            if irrelevant is not None:
                if irrelevant is True:
                    bad_nodes.append(x)
        terminal_states_ids = [mapping[x] for x in bad_nodes]
        if len(terminal_states_ids) != 0:
            # for node in self.graph.nodes:
            #     if node in bad_nodes:
            #         continue
            starting_id = mapping[stats.root]
            sum = 0
            # for terminal_state_id in terminal_states_ids:
            #     minmin, minmax, maxmin, maxmax = extract_probabilities([terminal_state_id], gateway, mc, mdp, root_id=starting_id)
            #     sum+=maxmax
            minmin, minmax, maxmin, maxmax = extract_probabilities(terminal_states_ids, gateway, mc, mdp, root_id=starting_id)
            if maxmin > 0.2:
                print(maxmin)

    def check_split(self, t, x, x_label, nn, bar_main, stats, template, template_2d) -> List[Experiment.SuccessorInfo]:
        # -------splitting
        ranges_probs = self.create_range_bounds_model(template, x, self.env_input_size, nn)
        split_flag = is_split_range(ranges_probs, self.max_probability_split)
        new_frontier = []
        if split_flag and self.use_split:
            # bar_main.update(value=bar_main.value + 1, current_t=t, last_action="split", last_polytope=str(x))
            to_split = []
            n_splits = 0
            to_split.append(x)
            bar_main.update(force=True)
            bar_main.fd.flush()
            print("", file=sys.stderr)  # new line
            # new_frontier = pickle.load(open("new_frontier.p","rb"))
            widgets = [progressbar.Variable('splitting_queue'), ", ", progressbar.Variable('frontier_size'), ", ", progressbar.widgets.Timer()]
            with progressbar.ProgressBar(prefix=f"Splitting states: ", widgets=widgets, is_terminal=True, term_width=200, redirect_stdout=True).start() as bar_split:
                while len(to_split) != 0:
                    bar_split.update(value=bar_main.value + 1, splitting_queue=len(to_split), frontier_size=len(new_frontier))
                    to_analyse = to_split.pop()
                    if self.use_entropy_split:
                        split1, split2 = sample_and_split(self.get_pre_nn(), nn, template, np.array(to_analyse), self.env_input_size, template_2d)
                        if split1 is None or split2 is None:
                            split1, split2 = sample_and_split(self.get_pre_nn(), nn, template, np.array(to_analyse), self.env_input_size, template_2d)
                    else:
                        dimension = pick_longest_dimension(template, x)
                        split1, split2 = split_polyhedron(template, x, dimension)
                    ranges_probs1 = self.create_range_bounds_model(template, split1, self.env_input_size, nn)
                    split_flag1 = is_split_range(ranges_probs1, self.max_probability_split) and self.use_split
                    if split_flag1:
                        to_split.append(split1)
                    else:
                        n_splits += 1
                        successor_info = Experiment.SuccessorInfo()
                        successor_info.successor = tuple(split1)
                        successor_info.parent = x
                        successor_info.parent_lbl = x_label
                        successor_info.t = t
                        successor_info.action = f"split{n_splits}"
                        new_frontier.append(successor_info)
                        # plot_frontier(new_frontier)
                    ranges_probs2 = self.create_range_bounds_model(template, split2, self.env_input_size, nn)
                    split_flag2 = is_split_range(ranges_probs2, self.max_probability_split)
                    if split_flag2:
                        to_split.append(split2)
                    else:
                        n_splits += 1
                        successor_info = Experiment.SuccessorInfo()
                        successor_info.successor = tuple(split2)
                        successor_info.parent = x
                        successor_info.parent_lbl = x_label
                        successor_info.t = t
                        successor_info.action = f"split{n_splits}"
                        new_frontier.append(successor_info)

                        # plot_frontier(new_frontier)
            # print("finished splitting")

            return ray.put(new_frontier)
        else:
            return None  # we return none in the case where there was no splitting

    @staticmethod
    def round_tuple(x, rounding_value):
        '''To be used only for template values, this is not a good rounding in other cases'''
        rounded_x = []
        for val in x:
            rounded_value = Experiment.round_single(val, rounding_value)
            rounded_x.append(rounded_value)
        return tuple(rounded_x)

    @staticmethod
    def round_single(val, rounding_value):
        '''To be used only for template values, this is not a good rounding in other cases'''
        if val < 0:
            rounded_value = -1 * math.floor(abs(val) * rounding_value) / rounding_value
        else:
            rounded_value = math.ceil(abs(val) * rounding_value) / rounding_value
        return rounded_value

    def generate_root_polytope(self):
        gurobi_model = grb.Model()
        gurobi_model.setParam('OutputFlag', self.output_flag)
        input = Experiment.generate_input_region(gurobi_model, self.input_template, self.input_boundaries, self.env_input_size)
        x_results = self.optimise(self.analysis_template, gurobi_model, input)
        if x_results is None:
            print("Model unsatisfiable")
            return None
        root = tuple(x_results)
        return root

    @staticmethod
    def generate_input_region(gurobi_model, templates, boundaries, env_input_size):
        input = gurobi_model.addMVar(shape=env_input_size, lb=float("-inf"), ub=float("inf"), name="input")
        Experiment.generate_region_constraints(gurobi_model, templates, input, boundaries, env_input_size)
        return input

    @staticmethod
    def generate_region_constraints(gurobi_model, templates, input, boundaries, env_input_size, invert=False):
        for j, template in enumerate(templates):
            gurobi_model.update()
            multiplication = 0
            for i in range(env_input_size):
                multiplication += template[i] * input[i]
            if not invert:
                gurobi_model.addConstr(multiplication <= boundaries[j], name=f"input_constr_{j}")
            else:
                gurobi_model.addConstr(multiplication >= boundaries[j], name=f"input_constr_{j}")

    def get_pre_nn(self):
        """Returns the transformation operation to transform from input to observation"""
        return torch.nn.Identity()

    def check_unsafe(self, template, bnds, x_label):
        for A, b in self.unsafe_zone:
            gurobi_model = grb.Model()
            gurobi_model.setParam('OutputFlag', False)
            input = gurobi_model.addMVar(shape=(self.env_input_size,), lb=float("-inf"), name="input")
            Experiment.generate_region_constraints(gurobi_model, template, input, bnds, self.env_input_size)
            Experiment.generate_region_constraints(gurobi_model, A, input, b, self.env_input_size)
            gurobi_model.update()
            gurobi_model.optimize()
            if gurobi_model.status == 2:
                return True
        return False

    @staticmethod
    def e(n, i):
        result = [0] * n
        result[min(i, n)] = 1
        return np.array(result)

    @staticmethod
    def octagon(n):
        template = []
        for i in range(n):
            x = Experiment.e(n, i)
            template.append(x)
            template.append(-x)
            for j in range(0, i):
                y = Experiment.e(n, j)
                template.append(x + y)
                template.append(x - y)
                template.append(y - x)
                template.append(-y - x)
        return np.stack(template)

    @staticmethod
    def combinations(items: List[np.ndarray]):
        template = []
        for i, item in enumerate(items):
            template.append(item)
            template.append(-item)
            for j in range(0, i):
                other = items[j]
                template.append(item + other)
                template.append(item - other)
                template.append(other - item)
                template.append(-other - item)
        return np.stack(template)

    @staticmethod
    def box(n):
        template = []
        for i in range(n):
            x = Experiment.e(n, i)
            template.append(x)
            template.append(-x)
        return np.stack(template)

    def h_repr_to_plot(self, gurobi_model, template, x_prime):
        x_prime_results = self.optimise(template, gurobi_model, x_prime)  # h representation
        return x_prime_results is not None, x_prime_results

    @staticmethod
    def generate_nn_guard(gurobi_model: grb.Model, input, nn: torch.nn.Sequential, action_ego=0, M=1e2):
        gurobi_model.setParam("DualReductions", 0)
        gurobi_vars = []
        gurobi_vars.append(input)
        for i, layer in enumerate(nn):

            # print(layer)
            if type(layer) is torch.nn.Linear:
                v = gurobi_model.addMVar(lb=float("-inf"), shape=(int(layer.out_features)), name=f"layer_{i}")
                lin_expr = layer.weight.data.numpy() @ gurobi_vars[-1]
                if layer.bias is not None:
                    lin_expr = lin_expr + layer.bias.data.numpy()
                gurobi_model.addConstr(v == lin_expr, name=f"linear_constr_{i}")
                gurobi_vars.append(v)
                gurobi_model.update()
                gurobi_model.optimize()
                assert gurobi_model.status == 2, "LP wasn't optimally solved"
            elif type(layer) is torch.nn.ReLU:
                v = gurobi_model.addMVar(lb=float("-inf"), shape=gurobi_vars[-1].shape, name=f"layer_{i}")  # same shape as previous

                z = gurobi_model.addMVar(shape=gurobi_vars[-1].shape, vtype=grb.GRB.BINARY, name=f"relu_{i}")  # lb=0, ub=1,
                # gurobi_model.addConstr(v == grb.max_(0, gurobi_vars[-1]))
                gurobi_model.addConstr(v >= gurobi_vars[-1], name=f"relu_constr_1_{i}")
                gurobi_model.addConstr(v <= gurobi_vars[-1] + M * z, name=f"relu_constr_2_{i}")
                gurobi_model.addConstr(v >= 0, name=f"relu_constr_3_{i}")
                gurobi_model.addConstr(v <= M - M * z, name=f"relu_constr_4_{i}")
                gurobi_vars.append(v)
                gurobi_model.update()
                gurobi_model.optimize()
                assert gurobi_model.status == 2, "LP wasn't optimally solved"
                """
                y = Relu(x)
                0 <= z <= 1, z is integer
                y >= x
                y <= x + Mz
                y >= 0
                y <= M - Mz"""
        # gurobi_model.update()
        # gurobi_model.optimize()
        # assert gurobi_model.status == 2, "LP wasn't optimally solved"
        # gurobi_model.setObjective(v[action_ego].sum(), grb.GRB.MAXIMIZE)  # maximise the output
        last_layer = gurobi_vars[-1]
        for i in range(last_layer.shape[0]):
            if i == action_ego:
                continue
            gurobi_model.addConstr(last_layer[action_ego] >= last_layer[i], name="last_layer")
        # if action_ego == 0:
        #     gurobi_model.addConstr(last_layer[0] >= last_layer[1], name="last_layer")
        # else:
        #     gurobi_model.addConstr(last_layer[1] >= last_layer[0], name="last_layer")
        gurobi_model.update()
        gurobi_model.optimize()
        # assert gurobi_model.status == 2, "LP wasn't optimally solved"
        return gurobi_model.status == 2 or gurobi_model.status == 5

    @staticmethod
    def generate_nn_guard_positive(gurobi_model: grb.Model, input, nn: torch.nn.Sequential, positive, M=1e2):
        gurobi_model.setParam("DualReductions", 0)
        gurobi_vars = []
        gurobi_vars.append(input)
        for i, layer in enumerate(nn):

            # print(layer)
            if type(layer) is torch.nn.Linear:
                v = gurobi_model.addMVar(lb=float("-inf"), shape=(int(layer.out_features)), name=f"layer_{i}")
                lin_expr = layer.weight.data.numpy() @ gurobi_vars[-1]
                if layer.bias is not None:
                    lin_expr = lin_expr + layer.bias.data.numpy()
                gurobi_model.addConstr(v == lin_expr, name=f"linear_constr_{i}")
                gurobi_vars.append(v)
                gurobi_model.update()
                gurobi_model.optimize()
                assert gurobi_model.status == 2, "LP wasn't optimally solved"
            elif type(layer) is torch.nn.ReLU:
                v = gurobi_model.addMVar(lb=float("-inf"), shape=gurobi_vars[-1].shape, name=f"layer_{i}")  # same shape as previous

                z = gurobi_model.addMVar(shape=gurobi_vars[-1].shape, vtype=grb.GRB.BINARY, name=f"relu_{i}")  # lb=0, ub=1,
                # gurobi_model.addConstr(v == grb.max_(0, gurobi_vars[-1]))
                gurobi_model.addConstr(v >= gurobi_vars[-1], name=f"relu_constr_1_{i}")
                gurobi_model.addConstr(v <= gurobi_vars[-1] + M * z, name=f"relu_constr_2_{i}")
                gurobi_model.addConstr(v >= 0, name=f"relu_constr_3_{i}")
                gurobi_model.addConstr(v <= M - M * z, name=f"relu_constr_4_{i}")
                gurobi_vars.append(v)
                gurobi_model.update()
                gurobi_model.optimize()
                assert gurobi_model.status == 2, "LP wasn't optimally solved"
                """
                y = Relu(x)
                0 <= z <= 1, z is integer
                y >= x
                y <= x + Mz
                y >= 0
                y <= M - Mz"""
        # gurobi_model.update()
        # gurobi_model.optimize()
        # assert gurobi_model.status == 2, "LP wasn't optimally solved"
        # gurobi_model.setObjective(v[action_ego].sum(), grb.GRB.MAXIMIZE)  # maximise the output
        last_layer = gurobi_vars[-1]
        if positive:
            gurobi_model.addConstr(last_layer[0] >= 0, name="last_layer")
        else:
            gurobi_model.addConstr(last_layer[0] <= 0, name="last_layer")
        gurobi_model.update()
        gurobi_model.optimize()
        # assert gurobi_model.status == 2, "LP wasn't optimally solved"
        return gurobi_model.status == 2 or gurobi_model.status == 5

    @staticmethod
    def generate_nn_guard_continuous(gurobi_model: grb.Model, input, nn: torch.nn.Sequential):
        gurobi_vars = []
        gurobi_vars.append(input)
        for i, layer in enumerate(nn):

            # print(layer)
            if type(layer) is torch.nn.Linear:
                v = gurobi_model.addMVar(lb=float("-inf"), shape=(int(layer.out_features)), name=f"layer_{i}")
                lin_expr = layer.weight.data.numpy() @ gurobi_vars[-1]
                if layer.bias is not None:
                    lin_expr = lin_expr + layer.bias.data.numpy()
                gurobi_model.addConstr(v == lin_expr, name=f"linear_constr_{i}")
                gurobi_vars.append(v)
            elif type(layer) is torch.nn.ReLU:
                v = gurobi_model.addMVar(lb=float("-inf"), shape=gurobi_vars[-1].shape, name=f"layer_{i}")  # same shape as previous
                z = gurobi_model.addMVar(lb=0, ub=1, shape=gurobi_vars[-1].shape, vtype=grb.GRB.INTEGER, name=f"relu_{i}")
                M = 1e3
                # gurobi_model.addConstr(v == grb.max_(0, gurobi_vars[-1]))
                gurobi_model.addConstr(v >= gurobi_vars[-1], name=f"relu_constr_1_{i}")
                gurobi_model.addConstr(v <= gurobi_vars[-1] + M * z, name=f"relu_constr_2_{i}")
                gurobi_model.addConstr(v >= 0, name=f"relu_constr_3_{i}")
                gurobi_model.addConstr(v <= M - M * z, name=f"relu_constr_4_{i}")
                gurobi_vars.append(v)
                # gurobi_model.update()
                # gurobi_model.optimize()
                # assert gurobi_model.status == 2, "LP wasn't optimally solved"
                """
                y = Relu(x)
                0 <= z <= 1, z is integer
                y >= x
                y <= x + Mz
                y >= 0
                y <= M - Mz"""
            elif type(layer) is torch.nn.Hardtanh:
                layerTanh: torch.nn.Hardtanh = layer
                min_val = layerTanh.min_val
                max_val = layerTanh.max_val
                M = 10e3
                v1 = gurobi_model.addMVar(lb=float("-inf"), shape=gurobi_vars[-1].shape, name=f"layer_{i}")  # same shape as previous
                z1 = gurobi_model.addMVar(lb=0, ub=1, shape=gurobi_vars[-1].shape, vtype=grb.GRB.INTEGER, name=f"hardtanh1_{i}")
                z2 = gurobi_model.addMVar(lb=0, ub=1, shape=gurobi_vars[-1].shape, vtype=grb.GRB.INTEGER, name=f"hardtanh2_{i}")
                gurobi_model.addConstr(v1 >= gurobi_vars[-1], name=f"hardtanh1_constr_1_{i}")
                gurobi_model.addConstr(v1 <= gurobi_vars[-1] + M * z1, name=f"hardtanh1_constr_2_{i}")
                gurobi_model.addConstr(v1 >= min_val, name=f"hardtanh1_constr_3_{i}")
                gurobi_model.addConstr(v1 <= min_val + M - M * z1, name=f"hardtanh1_constr_4_{i}")
                gurobi_vars.append(v1)
                v2 = gurobi_model.addMVar(lb=float("-inf"), shape=gurobi_vars[-1].shape, name=f"layer_{i}")  # same shape as previous
                gurobi_model.addConstr(v2 <= gurobi_vars[-1], name=f"hardtanh2_constr_1_{i}")
                gurobi_model.addConstr(v2 >= gurobi_vars[-1] - M * z2, name=f"hardtanh2_constr_2_{i}")
                gurobi_model.addConstr(v2 <= max_val, name=f"hardtanh2_constr_3_{i}")
                gurobi_model.addConstr(v2 >= max_val - M + M * z2, name=f"hardtanh2_constr_4_{i}")
                gurobi_vars.append(v2)
            else:
                raise Exception("Unrecognised layer")
        gurobi_model.update()
        gurobi_model.optimize()
        assert gurobi_model.status == 2, "LP wasn't optimally solved"
        last_layer = gurobi_vars[-1]
        gurobi_model.setObjective(last_layer[0].sum(), grb.GRB.MAXIMIZE)  # maximise the output
        gurobi_model.update()
        gurobi_model.optimize()
        assert gurobi_model.status == 2, "LP wasn't optimally solved"
        max_val = gurobi_model.ObjVal
        gurobi_model.setObjective(last_layer[0].sum(), grb.GRB.MINIMIZE)  # maximise the output
        gurobi_model.update()
        gurobi_model.optimize()
        assert gurobi_model.status == 2, "LP wasn't optimally solved"
        min_val = gurobi_model.ObjVal
        return last_layer, max_val, min_val

    def generic_plot(self, title_x, title_y, vertices_list, template, template_2d):
        fig, simple_vertices = show_polygon_list3(vertices_list, title_x, title_y, template, template_2d)
        if self.show_progress_plot:
            fig.show()
        if self.save_dir is not None:
            width = 2560
            height = 1440
            scale = 1
            fig.write_image(os.path.join(self.save_dir, "plot.svg"), width=width, height=height, scale=scale)
            fig.write_image(os.path.join(self.save_dir, "plot.png"), width=width, height=height, scale=scale)
            fig.write_image(os.path.join(self.save_dir, "plot.jpeg"), width=width, height=height, scale=scale)
            fig.write_image(os.path.join(self.save_dir, "plot.pdf"), width=width, height=height, scale=scale)
            fig.write_html(os.path.join(self.save_dir, "plot.html"), include_plotlyjs="cdn")
            fig.write_json(os.path.join(self.save_dir, "plot.json"))
            with open(os.path.join(self.save_dir, "plot.csv"), 'w', newline='') as myfile:
                wr = csv.writer(myfile, quoting=csv.QUOTE_NONNUMERIC)
                for timestep in simple_vertices:
                    for item in timestep:
                        # assert len(item) == 4
                        for vertex in item:
                            wr.writerow(vertex)
                        wr.writerow(item[0])  # write back the first item
                    wr.writerow("")

    def generic_plot1d(self, title_x, title_y, vertices_list, template, template_2d):
        fig, simple_vertices = show_polygon_list31d(vertices_list, title_x, title_y, template, template_2d)
        if self.show_progress_plot:
            fig.show()
        if self.save_dir is not None:
            width = 2560
            height = 1440
            scale = 1
            fig.write_image(os.path.join(self.save_dir, "plot.svg"), width=width, height=height, scale=scale)
            fig.write_image(os.path.join(self.save_dir, "plot.png"), width=width, height=height, scale=scale)
            fig.write_image(os.path.join(self.save_dir, "plot.jpeg"), width=width, height=height, scale=scale)
            fig.write_image(os.path.join(self.save_dir, "plot.pdf"), width=width, height=height, scale=scale)
            fig.write_html(os.path.join(self.save_dir, "plot.html"), include_plotlyjs="cdn")
            fig.write_json(os.path.join(self.save_dir, "plot.json"))
            with open(os.path.join(self.save_dir, "plot.csv"), 'w', newline='') as myfile:
                wr = csv.writer(myfile, quoting=csv.QUOTE_NONNUMERIC)
                for timestep in simple_vertices:
                    for item in timestep:
                        # assert len(item) == 4
                        for vertex in item:
                            wr.writerow(vertex)
                        wr.writerow(item[0])  # write back the first item
                    wr.writerow("")


def contained(x: tuple, y: tuple, eps=1e-9):
    # y contains x
    assert len(x) == len(y)
    for i in range(len(x)):
        if x[i] > y[i] + eps:
            return False
    return True


def find_contained(x_info: Experiment.SuccessorInfo, seen):
    for i, i_label in seen:
        if contained(x_info.successor, i) and x_info.successor_lbl == i_label:
            return True, (i, i_label)
    return False, -1
