"""The Timesheets agent (see manifest.yaml).

Where the week went, by project: hours logged by hand or turned from
calendar meetings by the user's own rules, a week summed with its gaps,
locked when it is handed in, and a workbook to send.
"""

from decentai_sdk.base import AgentBase

from .tools import EntriesTool, ProjectsTool, WeeksTool


class TimesheetsAgent(AgentBase):
    def tools(self):
        return [ProjectsTool(self), EntriesTool(self), WeeksTool(self)]
