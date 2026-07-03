# Repository Guidelines

This document serves as a contributor guide for the ailocal repository, providing essential information about the project's structure, development workflow, and best practices.

## Project Structure & Module Organization

The repository is organized as follows:
- `scripts/` - Core setup and management scripts including install.sh, start.sh, stop.sh, and healthcheck.sh
- `config/` - Configuration: `litellm/` (proxy config), `clients/` (client templates), `profiles/` (per-RAM model presets), `models.yaml` (active profile)
- `data/` - Data storage directory
- `logs/` - Log file storage
- `backups/` - Backup storage directory
- `.env.example` - Example environment variables file
- `docker-compose.yml` - Docker Compose configuration for all services

## Build, Test, and Development Commands

Key commands for development:
- `./scripts/install.sh` - Install host dependencies and generate .env file
- `ollama serve` - Start Ollama service (required for local AI inference)
- `./scripts/install-models.sh` - Pull required AI models (~85 GB on the 64 GB profile; 13–135 GB by tier)
- `./scripts/start.sh` - Start all Docker services
- `./scripts/healthcheck.sh` - Verify all services are running properly
- `./scripts/stop.sh` - Stop all services (preserves volumes)
- `./scripts/teardown.sh` - Full removal of containers, volumes, and network

## Coding Style & Naming Conventions

This project uses shell scripts for most operations with the following conventions:
- Scripts use bash with POSIX compliance
- All commands are designed to be run from the repository root
- Environment variables are loaded via `.env` files
- Service ports are bound to localhost only for security
- Model role names (router, reasoner, coder, supervisor, embed) are used instead of backend model names

## Testing Guidelines

The project relies on manual health checks through:
- `./scripts/healthcheck.sh` - Verifies all services are running properly
- Docker logs inspection for troubleshooting container issues
- Manual testing of API endpoints using curl or client tools

## Commit & Pull Request Guidelines

- Use descriptive commit messages following conventional commit format
- PR descriptions should clearly state the purpose and impact of changes
- All changes should be tested with the healthcheck script before submission
- When modifying environment files, ensure examples are updated accordingly

## Security & Configuration Tips

The stack is a single LiteLLM container bound to localhost (127.0.0.1:4000), authenticated by LITELLM_MASTER_KEY. For LAN exposure: put an authenticating reverse proxy in front, rotate LITELLM_MASTER_KEY to a strong value, and never expose port 4000 directly.

## Role-based Routing

All orchestration uses role names instead of backend model names:
- `router`: qwen3.5:9b-mlx - Fast classification, trivial tasks, autocomplete
- `reasoner`: deepseek-r1:32b - Planning, decomposition, deep reasoning  
- `coder`: qwen3.6:35b-mlx - Implementation, generation, coding tasks
- `supervisor`: gemma4:31b-mxfp8 - Review, critique, approval gate
- `embed`: nomic-embed-text - Semantic retrieval and memory

Never reference backend model names directly in client configs or scripts.

## Imported Claude Cowork project instructions

Local AI stack: Ollama models (qwen3.5-mlx, deepseek-r1, gemma4-mxfp8) behind LiteLLM proxy with role-based routing (router/coder/reasoner/supervisor). See README.md.
