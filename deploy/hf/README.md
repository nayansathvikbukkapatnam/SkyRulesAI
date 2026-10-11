---
title: SkyRules AI
emoji: 🛩️
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Cited answers about U.S. drone rules from official sources
---

# SkyRules AI (public demo)

A retrieval-augmented chatbot that answers questions about U.S. drone rules using only
the GovInfo edition of 14 CFR, the FAA's Federal Register drone rules and Know Before
You Fly guidance. Every sentence cites its source, and it refuses when the sources do
not support an answer.

This is a student project for MSAI 633, **not official FAA guidance**. For where you may
fly, use the FAA's B4UFLY and UAS Facility Maps.

Code, evaluation and reports: https://github.com/nayansathvikbukkapatnam/SkyRulesAI

The public demo is rate limited (questions per hour per visitor and a daily budget).
