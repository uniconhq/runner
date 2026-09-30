"""The socket filter: a policy standing between a grading run's harness and the
machine's Docker. It holds the daemon's socket and gives the harness one of
its own, reads every request on it and passes only what a grading run needs.
Standard library only.
"""
