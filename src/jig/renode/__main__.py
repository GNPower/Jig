"""``python -m jig.renode``: Jig's Renode commands without the rest of the CLI."""

import sys

from jig.renode.commands import main

sys.exit(main())
