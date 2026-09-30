"""The program inside the Unicon harness image. The CI starts it once per
grading run: it reads the envelope it is pointed at, runs the compiled plan
from the task checkout over the submission checkout, one sandboxed container
per step through the socket filter, and posts the verdict back.
"""
