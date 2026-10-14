"""The bridge's local setup UI: a small web wizard served *by the bridge* on the
school LAN. A technician opens it in a browser, it finds the access-control
terminals on the local network, they type each terminal's admin password, and it
writes the config file — no hand-editing TOML.

This is deliberately separate from the SchoolOS web app: it runs on the mini-PC,
only over the LAN, and its secrets (terminal passwords) never leave the machine.
"""
