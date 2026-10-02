"""The Expenses agent (see manifest.yaml).

Receipts into a claim with every extracted fact checked against the
receipt's own text, assumptions kept apart from facts, duplicates
matched in code, the policy as rules with quoted sources, and a
status that advances to approved or paid only on a reviewer's word.
"""

from decentai_sdk.base import AgentBase

from .tools import ClaimsTool, PoliciesTool, ReceiptsTool


class ExpensesAgent(AgentBase):
    def tools(self):
        return [ClaimsTool(self), ReceiptsTool(self), PoliciesTool(self)]
