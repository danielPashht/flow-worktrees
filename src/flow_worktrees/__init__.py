"""flow — one git worktree per task; stage and ball derived from GitLab; a journal that writes itself."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("flow-worktrees")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
