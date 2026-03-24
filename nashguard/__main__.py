"""
Allow running NashGuard as a module:

    python -m nashguard           → starts MCP server (primary mode)
    python -m nashguard --cli     → interactive terminal (optional)
"""
import sys

if "--cli" in sys.argv:
    sys.argv.remove("--cli")
    from .main import main
    main()
else:
    from .server import run_server
    run_server()
