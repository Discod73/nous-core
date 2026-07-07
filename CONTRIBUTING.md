# Contributing to NOUS

Thanks for your interest in NOUS. This project started as a personal, local-first AI system for my own family, and it's now open source, so others can build similar systems without sending their data to the cloud.

It's not a polished product. It's a working experiment that I actively use and improve. Contributions are welcome, but please read this before opening a PR.

## Before you contribute

- **Open an issue first for anything non-trivial.** If you want to add a feature, change architecture, or refactor something significant, open an issue describing what and why before writing code, this saves you time if the change doesn't fit the project's direction.
- **Bug fixes and small improvements can go straight to a PR.** Typos, broken commands, dependency fixes, clearer error messages, just send the PR.
- **Hardware-specific support is welcome.** The project was built and tested on Raspberry Pi 5 + Jetson Orin NX (aarch64). If you're adding x86_64 support, alternate inference backends, or different hardware targets, please keep the existing aarch64 path working in parallel rather than replacing it. Use environment variables or config files to make new behavior opt-in, not default.

## What I'm looking for

- Hardware compatibility improvements (x86_64, other ARM boards, different GPU backends)
- Bug fixes, especially around install.sh and the voice pipeline
- Documentation improvements
- Internationalization, the collection names, prompts, and some UI strings are Danish-specific. Making these configurable via environment variables (rather than hardcoded) is valuable.

## What I'm cautious about

- Large architectural changes without prior discussion
- Anything that adds a hard cloud dependency (this project's entire point is staying local-first)
- Changes that remove support for the original hardware target without a fallback

## Submitting a PR

1. Fork the repo and create a branch from `main`.
2. Make your changes. Keep commits focused, one logical change per commit if possible.
3. Test on your own hardware before submitting. Note your test setup (hardware, OS, model used) in the PR description.
4. Open the PR against `Discod73/nous-core` directly, not against a fork. This makes review and merge cleaner.
5. Be patient, this is maintained by one person in their spare time. I'll review as soon as I can.

## Code style

- No strict linter is enforced yet. Match the style of the surrounding code.
- Comments and prompts in the core system are currently a mix of Danish and English. New contributions should default to English unless you're working on Danish-specific functionality (e.g. the Legal Engine, which is intentionally Danish).

## Reporting bugs

Open an issue with:
- What you expected to happen
- What actually happened
- Your hardware (Pi/Jetson model, RAM, etc.)
- Relevant logs (`sudo journalctl -u nous-api.service -n 50`, or whichever service is affected)

## Questions

Open a GitHub Discussion or issue. I'm not always fast to respond, but I do read everything.

Thanks for taking the time to look at this project.
