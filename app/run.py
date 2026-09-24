"""进程入口：python run.py api|executor|instrument"""
import runpy
import sys


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("api", "executor", "instrument"):
        sys.stderr.write("用法: run.py api|executor|instrument\n")
        sys.exit(2)
    runpy.run_module(f"{sys.argv[1]}", run_name="__main__")


if __name__ == "__main__":
    main()
