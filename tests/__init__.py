"""Ssokly test suite."""
"""Tests never select the deployed relay implicitly from desktop configuration."""
import os

os.environ['SSOKLY_API_URL'] = ''
