"""Constants for the Second Hand integration."""

DOMAIN = "second_hand"

CONF_ADDRESS = "address"
CONF_ADAPTER = "adapter"
CONF_ASSIST_DEVICES = "assist_devices"
CONF_HELPERS = "helpers"
CONF_TIMERS_ONLY = "timers_only"

ALLOW_TIMERS_ONLY = False
"""Off for users. Setup without a clock (the Timers sensor only) stays in the code for testing; set True to get it back."""

POLL_SECONDS = 10
"""Safety-net re-read of the timer sources, on top of their change callbacks."""

SEARCH_SECONDS = 30
PAIR_SECONDS = 180

ISSUE_ASSIST_UNSUPPORTED = "assist_timers_unsupported"
