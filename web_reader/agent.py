"""The Web Reader agent (see manifest.yaml).

Reads a public web page or PDF with the Documents rules: the words in
numbered sections or pages, quoted with where they came from, never
invented; and saves a copy as a platform file another agent can read.
Stateless and without a credential: every call fetches afresh, and only
public internet addresses are ever fetched (tools/fetch.py).
"""

from decentai_sdk.base import AgentBase

from .tools import PagesTool


class WebReaderAgent(AgentBase):
    def tools(self):
        return [PagesTool(self)]
