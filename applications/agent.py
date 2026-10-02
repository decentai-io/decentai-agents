"""The Applications agent (see manifest.yaml).

A guided application: the form owns the sequence of steps, every answer
and document is checked as it arrives, the application is a record the
person can open without the agent, and submission is the person's own
final step.
"""

from decentai_sdk.base import AgentBase

from .tools import ApplicationsTool, FormsTool


class ApplicationsAgent(AgentBase):
    def tools(self):
        return [FormsTool(self), ApplicationsTool(self)]
