# Ghost Routes

Verify how construction phases, overlapping projects, and plan revisions affect pedestrian access to essential destinations.

**Status: design handoff. The application has not been implemented or tested yet.**

The proposed system converts construction plans into source-linked observations, lets a reviewer confirm a pedestrian network, checks explicit access requirements, and evaluates candidate repairs. Results describe the reviewed model and its coverage, not real-world safety or legal compliance.

## Start here

- [BUILD_PLAN.md](BUILD_PLAN.md): complete implementation contract, architecture, competitor analysis, open-source foundations, milestones, and acceptance criteria.
- [CLAUDE.md](CLAUDE.md): instructions for the implementing agent.

The handoff is on `docs/claude-build-plan`. Start implementation from that branch without waiting for its PR to merge. Do not interpret planned features as implemented capabilities.

## Credentials

The planned core runs locally using PostgreSQL/PostGIS and an ungated open-weight vision model through Ollama. It requires no cloud API key, paid map service, application login, or Hugging Face access token. Initial dependency, model, and public-document downloads need internet access. The coding assistant needs its own configured authentication.

## License

Original code and documentation are MIT licensed. Third-party code, model weights, schema materials, map data, and source plan documents retain their respective licenses and attribution requirements. See the reuse and data rules in the build plan.
