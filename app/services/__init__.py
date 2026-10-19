"""Business layer: every Cypher query of the API lives in a service class.

Routers and agent tools call these methods; their signatures are the team contract: never rename or change them.
A method that is not written yet raises NotImplementedError, which the API turns into HTTP 501.
"""
