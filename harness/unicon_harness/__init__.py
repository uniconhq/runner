"""The program inside the Unicon harness image. The CI starts it once per
grading run: it reads the envelope it is pointed at, runs the compiled plan
from the publication checkout over the submission checkout, and posts a
verdict back. Only the first of those exists today; the plan runner and the
sandbox are feature 06.
"""
