"""支持 python -m hushai_agent 方式启动。"""

import sys

from hushai_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
