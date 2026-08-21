# IDEA.md

**A tiny CLI tool that roasts your git commit messages using a local LLM.**

- Parse the last N commits from any git repo and feed them to a lightweight model for evaluation.
- Score commits on clarity, humor, and vagueness; flag messages like "stuff" or "fix things" for maximum roasting.
- Output a leaderboard of your worst commits with sarcastic commentary and a "commit hygiene" grade.
- Support custom roast styles (corporate, pirate, Shakespearean) via prompt templates.
- Track improvement over time with a simple local history file so you can watch your commits get less terrible.
