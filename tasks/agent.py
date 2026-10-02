"""The Tasks agent (see manifest.yaml).

What you have to do, and everything that renews or expires, in one
list. Proposals come out of notes with a quote behind each and become
tasks only when a person confirms them; owners and dates are never
invented. Renewals keep two dates apart — when it renews, and the
earlier day notice is due — and that arithmetic is code, in calendar
or business days. Overdue and upcoming are both fit for a clock.
"""

from decentai_sdk.base import AgentBase

from .tools import AgreementsTool, DeadlinesTool, DecisionsTool, ExtractTool, TasksTool


class TasksAgent(AgentBase):
    def tools(self):
        return [TasksTool(self), ExtractTool(self), AgreementsTool(self),
                DeadlinesTool(self), DecisionsTool(self)]
