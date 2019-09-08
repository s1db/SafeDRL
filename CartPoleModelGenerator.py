import numpy as np
import torch
import zmq
from gym.envs.classic_control import CartPoleEnv

from dqn_agent import Agent
from proto.state_request_pb2 import StringVarNames, StateInt

state_size = 4
action_size = 2
ALPHA = 0.4


class CartPoleModelGenerator:
    def __init__(self, port: int):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(f"tcp://*:{port}")
        self.last_state: np.ndarray
        self.last_action: np.ndarray
        self.p: float = 0.8  # probability of not sticky actions
        self.max_n: int = 12
        self.current_t: int = 0
        self.failed: bool = False
        self.terminal: bool = False
        self.env = CartPoleEnv()
        self.env.seed(0)
        self.agent = Agent(state_size=state_size, action_size=action_size, alpha=ALPHA)
        self.agent.qnetwork_local.load_state_dict(torch.load('model.pth'))

    def start(self):
        while True:
            message: list = self.socket.recv_multipart()  # receives a multipart message
            decode = message[0].decode('utf-8')
            method_name = decode
            method = getattr(self, method_name, self.invalid_method)
            # Call the method as we return it
            method(message)

    def invalid_method(self, message):
        print("Invalid method name")
        self.socket.send_string("Invalid")

    def getVarNames(self, message):
        string_var_names = StringVarNames()
        string_var_names.value.append("x1")
        string_var_names.value.append("x2")
        string_var_names.value.append("x3")
        string_var_names.value.append("x4")
        self.socket.send(string_var_names.SerializeToString())

    def getVarTypes(self, message):
        string_var_names = StringVarNames()
        string_var_names.value.append("TypeInt")  # todo make it float?
        self.socket.send(string_var_names.SerializeToString())

    def getLabelNames(self, message):
        label_names = ["failed", "done"]
        string_var_names = StringVarNames()
        for label in label_names:
            string_var_names.value.append(label)
        self.socket.send(string_var_names.SerializeToString())

    def createVarList(self, message):
        pass

    def getInitialState(self, message):
        self.last_state = self.env.reset()
        self.current_t = 0
        self.done = False
        state = StateInt()
        for x in self.last_state:
            state.value.append(int(x * 100))  # multiply by 100 for rounding up integers
        self.socket.send(state.SerializeToString())

    def exploreState(self, message):
        state = StateInt()
        state.ParseFromString(message[1])
        self.last_state = self.parseFromPrism(state)  # parse from StateInt
        self.env.state = self.last_state  # loads the state in the environment
        self.current_t = None  # todo get t from the message
        self.socket.send_string("OK")

    def getNumChoices(self, message):
        self.socket.send_string("1")  # returns only 1 choice

    def getNumTransitions(self, message):
        if self.current_t > self.max_n:  # if done
            self.socket.send_string(str(1))
        self.socket.send_string(str(2))

    def getTransitionAction(self, message):
        pass

    def getTransitionProbability(self, message):
        i = int(message[1].decode('utf-8'))
        offset = int(message[2].decode('utf-8'))
        prob = 1.0 if (self.current_t > self.max_n) else (1 - self.p if offset == 0 else self.p)
        self.socket.send_string(str(prob))

    def computeTransitionTarget(self, message):
        state = StateInt()
        i = int(message[1].decode('utf-8'))
        offset = int(message[2].decode('utf-8'))
        if self.current_t >= self.max_n:
            # do nothing
            state.value.append(self.x)  # append the current state values
        else:
            action = self.agent.act(self.last_state, 0)
            state, reward, done, _ = self.env.step(action)
            self.last_state = state
            self.terminal = done
            self.current_t = self.current_t + 1
            if offset == 1 and not done:
                state, reward, done, _ = self.env.step(action)
                self.last_state = state
                self.terminal = done
            # todo convert the current new state to a state message
        self.socket.send(state.SerializeToString())

    def isLabelTrue(self, message):
        i = int(message[1].decode('utf-8'))
        if i == 0:
            value = self.done  # todo add fail condition
        elif i == 1:
            value = self.done
        else:
            # should never happen
            value = False
        self.socket.send_string(str(value))
        pass

    def getRewardStructNames(self, message):
        reward = StringVarNames()
        reward.value.append("r")
        self.socket.send(reward.SerializeToString())

    def getStateReward(self, message):
        self.socket.send_string(str(1.0))

    def getStateActionReward(self, message):
        self.socket.send_string(str(0.0))
    def parseFromPrism(self,state:StateInt):
        return np.ndarray(state.value,dtype=float)/100

if __name__ == '__main__':
    model = CartPoleModelGenerator(5558)
    model.start()
