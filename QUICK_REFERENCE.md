# Smart AI Router ↔ GBrain Integration - Quick Reference

## TL;DR

**Can smart-ai-router integrate with gbrain?** 
✅ **Yes.** They're natural partners. Three integration levels, starting with 1-2 weeks.

**Best approach?**
1. **Week 1-2:** Phase 1 - Smart-router as a GBrain "provider recipe"
2. **Week 3-4:** Phase 2 - Full routing configuration management
3. **Week 5-10:** Phase 3 - Integrated routing engine in GBrain gateway

---

## One-Page Architecture

```
TODAY:
smart-ai-router (standalone Python service)
    ↓ provides
OpenAI-compatible endpoint
    ↓ used by
[Cursor, Claude Code, custom apps]

PHASE 1 (1-2 weeks):
smart-ai-router (standalone Python service)
    ↓ consumed by
GBrain AI Gateway
    ↓ used by
[GBrain agents, skills, MCP plugins]

PHASE 3 (5-10 weeks total):
GBrain Gateway + Integrated Routing Engine
    ↓ includes
smart-ai-router's classification + routing logic
    ↓ manages
[OpenRouter, Ollama, Bedrock]
```

---

## Core Competencies

| Dimension | smart-ai-router | gbrain | Combined |
|-----------|-----------------|--------|----------|
| **Prompt Classification** | ✅ Excellent | Basic | Excellent |
| **Provider Routing** | Limited (cost only) | Sophisticated | Excellent |
| **Skills/Agents** | None | ✅ Excellent | Excellent |
| **Cost Optimization** | ✅ Excellent | Basic | Excellent |
| **Evaluation** | None | ✅ Framework | Excellent |
| **Config System** | Basic | ✅ Sophisticated | Excellent |
| **MCP Plugins** | None | ✅ Full | Full |

---

## Phase 1: What to Build

### smart-ai-router side (2-3 hours)
```python
# smart_ai_router/api/proxy.py

# Add response headers:
headers = {
    'X-Model-Used': selected_model.name,
    'X-Actual-Cost': str(estimated_cost),
    'X-Routing-Decision': f'Selected {model} for {profile.complexity}',
    'X-Domain-Profile': json.dumps(profile.__dict__),
}
```

### gbrain side (6-7 hours)
```typescript
// gbrain/src/core/ai/recipes/smart-router.ts

export const smartRouterRecipe: Recipe = {
  id: 'smart-router',
  type: 'native-openai-compatible',
  baseUrl: () => 'http://localhost:8001/v1',
  afterResponse: async (response) => {
    // Parse X-Model-Used, X-Actual-Cost, X-Routing-Decision headers
    // Return routing metadata
  }
}
```

### Result:
```json
// .gbrainrc.json
{
  "provider": "smart-router"
}
```

---

## Integration Points (All Phases)

| Component | Phase 1 | Phase 2 | Phase 3 |
|-----------|---------|---------|---------|
| **Recipe System** | ✅ Register | Enhance | Deepen |
| **Config** | Basic | ✅ Full | Config-based |
| **Gateway** | ✅ Call out | Track | Integrated |
| **Cost Tracking** | ✅ Headers | Unified | Unified |
| **Classifier** | External | External | ✅ Integrated |
| **Competence Scorer** | External | External | ✅ Merged |
| **Evaluations** | None | Link | ✅ Shared |
| **MCP Plugin** | None | Basic | ✅ Full |

---

## Key Files (By Phase)

### Phase 1 (10 files)
**Create (4):**
- `gbrain/src/core/ai/recipes/smart-router.ts`
- `gbrain/tests/recipes/smart-router.test.ts`
- `gbrain/docs/smart-router-integration.md`
- `smart-ai-router/docs/gbrain-integration.md`

**Modify (6):**
- `smart_ai_router/api/proxy.py` (+response headers)
- `gbrain/src/core/ai/gateway.ts` (+recipe registration)
- `gbrain/src/core/ai/types.ts` (+SmartRouterMetadata type)
- `gbrain/.gbrainrc.example.json` (+provider option)
- `gbrain/README.md` (+section on smart-router)
- `smart-ai-router/README.md` (+section on gbrain)

### Phase 2 (add ~10 files)
**Create (5):**
- `gbrain/src/core/ai/routing-config.ts`
- `gbrain/src/cli/commands/routing.ts`
- `gbrain/docs/routing-configuration.md`
- `gbrain/tests/routing-config.test.ts`
- `smart-ai-router/src/gbrain-config-schema.ts`

**Modify (8):**
- `gbrain/src/config/gbrainconfig.ts`
- `gbrain/src/core/ai/gateway.ts`
- `smart_ai_router/settings.py`
- etc.

### Phase 3 (add ~15 files)
**Create (10):**
- `gbrain/src/core/ai/prompt-profile.ts`
- `gbrain/src/core/ai/prompt-classifier.ts`
- `gbrain/src/core/ai/competence-scorer.ts`
- `gbrain/src/core/ai/cost-optimizer.ts`
- `gbrain/src/core/ai/evaluation-sync.ts`
- `gbrain/.codex-plugin/routing-tools.ts`
- `gbrain/evals/routing-eval.ts`
- `smart-ai-router/src/gbrain-schemas.ts`
- Tests for new modules
- Documentation

**Modify (10):**
- `gbrain/src/core/ai/gateway.ts` (major)
- `smart_ai_router/api/proxy.py` (minor)
- Various recipe files
- etc.

---

## Timeline at a Glance

```
Week 1  ████░░░░░░░░░░░░░░░░░░░░░░░░░░  Phase 1 (Recipe)
Week 2  ██████████████████░░░░░░░░░░░░░░  Phase 1 (Done)
Week 3  ░░░░░░░░████░░░░░░░░░░░░░░░░░░░░  Phase 2 (Config)
Week 4  ░░░░░░░░██████████░░░░░░░░░░░░░░  Phase 2 (Done)
Week 5  ░░░░░░░░░░░░░░░░░████░░░░░░░░░░░  Phase 3 (Classifier)
Week 6  ░░░░░░░░░░░░░░░░░██████░░░░░░░░░  Phase 3 (Scoring)
Week 7  ░░░░░░░░░░░░░░░░░███████████░░░░░  Phase 3 (Integration)
Week 8  ░░░░░░░░░░░░░░░░░██████████████░░  Phase 3 (Testing)
Week 9  ░░░░░░░░░░░░░░░░░██████████████░░  Phase 3 (Testing)
Week 10 ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  Phase 3 (Done)
```

---

## Success Checklist

### Phase 1 ✓
- [ ] Recipe file created and tested
- [ ] Response headers in smart-router
- [ ] Gateway imports and uses recipe
- [ ] Cost tracking captures actual costs
- [ ] Health check works
- [ ] Docs updated
- [ ] User can configure in `.gbrainrc.json`

### Phase 2 ✓
- [ ] Config schema extended
- [ ] Routing rules in config file
- [ ] CLI tool to edit config
- [ ] Cost thresholds work per-domain
- [ ] Fallback chains configurable
- [ ] Backward compatibility maintained

### Phase 3 ✓
- [ ] Classifier ported and integrated
- [ ] Competence scores computed in-process
- [ ] Cost optimizer works same as Phase 1
- [ ] No performance regression
- [ ] Evals show quality maintained
- [ ] No external smart-router service needed

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Smart-router down | Low | Medium | Fallback to other providers, health check |
| TypeScript port bugs | Medium | Medium | Test both in parallel, run evals |
| Config complexity | Low | Low | Docs, examples, CLI helpers |
| Performance hit | Low | Low | Benchmark, profile, optimize |
| User adoption | Medium | Low | Clear docs, obvious benefits, easy setup |

---

## Start Here

### If you want to discuss:
1. Read: `INTEGRATION_SUMMARY.md`
2. Read: `GBRAIN_INTEGRATION_PLAN.md`
3. Reach out with questions

### If you want to implement:
1. Read: `PHASE1_IMPLEMENTATION.md`
2. Create smart-router recipe file
3. Add response headers
4. Integrate with GBrain
5. Test
6. Ship Phase 1

### If you want detailed architecture:
1. Read: `GBRAIN_INTEGRATION_PLAN.md` (full context)
2. Read: `PHASE1_IMPLEMENTATION.md` (practical guide)
3. Reference: `QUICK_REFERENCE.md` (this file)

---

## Decision Tree

**Q: Should we do this?**
A: ✅ Yes. Low risk, high reward, aligns both projects.

**Q: Which phase first?**
A: Phase 1. Delivers value in 2 weeks, validates approach, low complexity.

**Q: Can we do it without breaking changes?**
A: ✅ Yes. All phases are additive, backward compatible.

**Q: What's the MVP?**
A: Phase 1. GBrain users can use smart-router, that's it.

**Q: What about TypeScript vs Python?**
A: Phase 1-2 use Python service. Phase 3 ports to TypeScript (optional).

**Q: Timeline?**
A: Phase 1 in 2 weeks, Phase 2 in 4 weeks, Phase 3 in 10 weeks.

**Q: Who owns what?**
A: Each project owns their code. Integration is at API boundaries.

---

## Numbers

- **Phase 1 Effort:** 40 hours (~2 weeks, 1 developer)
- **Phase 1 Risk:** Low (loose coupling)
- **Phase 1 Value:** High (cost optimization for all users)
- **Files Changed:** ~10 files
- **Breaking Changes:** 0
- **External Dependencies:** smart-ai-router service (for Phase 1-2)

---

## Contacts & Resources

**Repositories:**
- smart-ai-router: https://github.com/gaaschk/smart-ai-router
- gbrain: https://github.com/garrytan/gbrain

**Documentation (this repo):**
- `INTEGRATION_SUMMARY.md` - High-level overview
- `GBRAIN_INTEGRATION_PLAN.md` - Full architecture & strategy
- `PHASE1_IMPLEMENTATION.md` - Step-by-step Phase 1 guide
- `QUICK_REFERENCE.md` - This file

---

## Final Recommendation

**Phase 1 (Recipe Integration) - Do this first:**
- ✅ Low risk, high reward
- ✅ Ships in 2 weeks
- ✅ Can be extended to Phase 2/3 later
- ✅ Validates approach with real usage
- ✅ No breaking changes
- ✅ Users get immediate value

**Estimate:** 40 hours of work, 2 weeks calendar time, 1 confident developer

**Go/No-go:** ✅ **GO** - Recommend proceeding with Phase 1
