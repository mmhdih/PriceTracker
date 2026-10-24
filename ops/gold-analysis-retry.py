#!/usr/bin/env python3
"""Hermes no-agent wrapper; the profile/target is verified and stored privately."""
import os

os.execvp('python3', ['python3', '/root/gold-tracker-bot/retry_watcher.py', '--apply'])
