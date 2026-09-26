"""CRM-notes logging — a chatbot feature.

Reads settled WhatsApp conversations from the bot's own Redis, summarizes them
(Llama via OpenRouter), and logs a call-prep note to the CRM (find-or-create the
lead). Runs as a flag-gated background task / CLI — never in the message-reply
path. The CRM is pluggable: see `crm/base.py` (add a provider, register it, set
CRM_PROVIDER). Phone numbers are written WITHOUT a leading '+'.
"""
