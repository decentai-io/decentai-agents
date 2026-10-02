"""The Code agent (see manifest.yaml).

It keeps nothing between calls: a call writes a program, puts it
before the person, runs it in a folder made for that run, hands back
what it printed and the files it made, and removes the folder.
"""

from decentai_sdk.base import AgentBase

from .tools import ProgramTool


class CodeRunnerAgent(AgentBase):
    def tools(self):
        return [ProgramTool(self)]
