# Agent Lab

Build a modern developer-focused SaaS application called AgentLab, an evaluation platform for conversational AI agents.

Objective: Help developers test AI agent behavior, identify failures, and compare evaluation results.

Technology: Use Lovable's recommended frontend stack, TypeScript, and Lovable Cloud for persistence.

Screens:

Dashboard — evaluation metrics, recent runs, pass rates, and failure categories.

Test Suite — create, edit, and organize test scenarios with expected responses and evaluation criteria.

Run Evaluation — choose a test suite and launch execution.

Results — display individual test outcomes, response quality, latency, and failure explanations.

Architecture requirements:

The AI evaluation engine will be implemented separately in Python using LangGraph and FastAPI.

Design the frontend to communicate with that backend through REST APIs.

Create a clean API client/service layer.

Initially provide clearly labeled synthetic demo data for the UI.

Do not simulate backend execution and present it as a real evaluation.

Include loading, success, and error states for API requests.

Keep API credentials server-side.

Design: A polished developer-tool interface inspired by modern observability platforms, with dark mode, clear data visualizations, professional typography, and intuitive navigation.

Start by presenting an implementation plan and data model before making changes. Prioritize the simplest working MVP.

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/fd6604e7-5627-4fc6-b0f1-a956133d03ab).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```
