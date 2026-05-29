"""
Loads all environment variables and defines project-wide constants.

All string literals that appear more than once — API base URLs, sheet tab
names, column indices, Drive folder names, retry counts, poll intervals,
rate-limit thresholds — are defined here as module-level constants. No
other module should contain hardcoded strings.

Reads from a .env file for local development (via python-dotenv) and from
process environment variables when running in GitHub Actions.

Also defines the PinterestAccount dataclass that groups all account-specific
config, making the script ready for multi-account support in a future version
without requiring code changes outside this module.
"""
