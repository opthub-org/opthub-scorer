# coding: utf-8
"""
Hypervolume indicator.
"""
import json
import os

from pygmo import hypervolume


def main():
    solution_to_score = json.loads(input())
    solutions_scored = json.loads(input())

    ys = [s["objective"] for s in solutions_scored]
    ys.append(solution_to_score["objective"])
    hv = hypervolume(ys)
    ref_point = json.loads(os.getenv("HV_REF_POINT", "[1, 1]"))
    score = hv.compute(ref_point)
    print(score)


if __name__ == "__main__":
    main()
