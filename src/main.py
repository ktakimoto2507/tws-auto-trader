from .utils.logger import get_logger
from .get_account import main as run_account
from .get_history import main as run_history
from .get_realtime import main as run_realtime

log = get_logger("main")

def main():
    log.info("Start pipeline: account -> history -> realtime")
    run_account()
    run_history()
    run_realtime()
    log.info("Done")

if __name__ == "__main__":
    main()
