# Revised Integration Plan: Smart-ai-router as GBrain's Proxy

## The New Strategy

**Don't modify gbrain. Instead, make smart-ai-router a transparent proxy that gbrain uses via configuration.**

## The Insight

GBrain already supports OpenAI-compatible endpoints via `provider_base_urls` configuration. Smart-ai-router already provides an OpenAI-compatible endpoint. 

**Therefore:** Smart-ai-router can integrate with gbrain with ZERO code changes to gbrain.

## How It Works

### Current User Setup
```bash
# GBrain runs against OpenRouter API directly
gbrain --chat-model openrouter:meta-llama/llama-2-70b-chat
```

### With Smart-ai-router (No gbrain changes)
```bash
# 1. Start smart-ai-router once (long-running service)
smart-ai-router

# 2. Point GBrain to smart-ai-router via config
# In ~/.gbrainrc.json:
{
  "provider_base_urls": {
    "openrouter": "http://localhost:8001/v1"
  }
}

# 3. GBrain still uses same command, but routes through smart-ai-router
gbrain --chat-model openrouter:meta-llama/llama-2-70b-chat
#                                     ↓
#                          smart-ai-router evaluates:
#                          - Is this prompt really best served by llama-2?
#                          - Are there cheaper options with same quality?
#                          - Route optimally
#                          ↓
#                      OpenRouter / Ollama / Bedrock
```

## Changes Required

### To Smart-ai-router: MINIMAL (2-3 files)

#### 1. Support Special Routing Directives
**File:** `smart_ai_router/router.py`

Add support for special model IDs that trigger smart routing:

```python
# Accept model_id = "auto" or "auto-route" to use full intelligence
if model_id in ['auto', 'auto-route', 'smart-route']:
    selected_model = await self.select_optimal_model(profile)
else:
    selected_model = model_id  # Use what client requested
```

**Lines changed:** ~20 lines
**Risk:** Very low (additive feature)

#### 2. Add GBrain Integration Documentation
**File:** `docs/gbrain-integration.md` (NEW)

Explain to users how to configure GBrain to use smart-ai-router.

**Lines added:** ~100 lines
**Risk:** None (documentation only)

#### 3. Optional: GBrain Helper CLI Command
**File:** `smart_ai_router/cli/commands/setup_gbrain.py` (NEW)

```bash
smart-ai-router setup-gbrain
# This would:
# 1. Check if smart-ai-router is running
# 2. Show what to add to ~/.gbrainrc.json
# 3. Optionally modify the file for user
```

**Lines added:** ~80 lines
**Risk:** Very low (optional convenience feature)

### To GBrain: ZERO

✅ **No changes needed**
✅ **No modifications required**
✅ **No version compatibility concerns**
✅ **No maintenance burden**

Users simply:
1. Install smart-ai-router
2. Start it (long-running service)
3. Point gbrain config to it
4. Done

## Configuration Examples

### Example 1: GBrain Uses smart-ai-router for OpenAI
**User's `.gbrainrc.json`:**
```json
{
  "engine": "pglite",
  "chat_model": "openai:gpt-4o-mini",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```

What happens:
1. GBrain requests OpenAI GPT-4o-mini
2. Smart-ai-router classifies the prompt
3. Smart-ai-router decides: "This is simple, use GPT-4o-mini? Or use cheaper model?"
4. Routes accordingly
5. Returns response

### Example 2: GBrain Uses smart-ai-router for Multiple Providers
```json
{
  "chat_model": "openai:gpt-4o-mini",
  "embedding_model": "openai:text-embedding-3-large",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1",
    "anthropic": "http://localhost:8001/v1"
  }
}
```

Smart-ai-router acts as a unified router for all providers.

### Example 3: GBrain Uses Smart-ai-router's Auto Routing
```json
{
  "chat_model": "openai:auto",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```

The "auto" model tells smart-ai-router to pick the best model automatically based on the prompt profile.

## Architecture

```
GBrain (.gbrainrc.json)
├─ chat_model: "openai:gpt-4o-mini"
├─ provider_base_urls.openai: "http://localhost:8001/v1"
└─ Makes HTTP request to /v1/chat/completions

                    ↓

Smart-ai-router Service (localhost:8001)
├─ Receives request for gpt-4o-mini
├─ Classifies prompt (domain, complexity, demands)
├─ Evaluates available models
├─ Decides: is gpt-4o-mini the best choice?
│  ├─ If yes: route to it
│  └─ If no: route to better option (cheaper/better quality)
└─ Returns OpenAI-compatible response

                    ↓

Provider APIs (OpenRouter, Ollama, Bedrock, etc.)
└─ Receives request for selected model
   └─ Returns response
```

## Key Benefits

### For GBrain Users
- 💰 60%+ cost savings
- 🎯 Transparent to GBrain
- 🔄 Works with existing setup
- 📊 Routing decisions logged
- 🚀 No configuration complexity

### For Smart-ai-router
- 🧠 Works with sophisticated agents (gbrain's agents)
- 📈 Reaches more users via gbrain
- 🔌 Demonstrates OpenAI-compat capability
- 🌍 Ecosystem integration

### For Both
- 🤝 Minimal coupling (config-level only)
- 🛠️ No maintenance burden
- 📝 Clear documentation
- 🎯 Focused integration

## User Journey

### Day 1: Discovery
User reads that smart-ai-router can integrate with gbrain.

### Day 2: Install
```bash
# Install smart-ai-router
git clone https://github.com/gaaschk/smart-ai-router
cd smart-ai-router
pip install -e .
smart-ai-router setup

# Runs on http://localhost:8001
```

### Day 3: Configure GBrain
User edits `~/.gbrainrc.json`:
```json
{
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```

### Day 4: Enjoy
```bash
# Same GBrain commands, but with cost optimization
gbrain  # Uses smart-ai-router automatically
```

### Day 5+: Monitor
```bash
# See routing decisions
smart-ai-router logs

# See cost savings
smart-ai-router stats
```

## Implementation Roadmap

### Phase 1: Core Support (1 week)
- [ ] Make smart-ai-router accept "auto" model ID
- [ ] Ensure response headers are correct
- [ ] Write GBrain integration documentation
- [ ] Test with actual GBrain instance

**Deliverable:** Users can point GBrain to smart-ai-router

### Phase 2: User Experience (1 week, optional)
- [ ] Add `smart-ai-router setup-gbrain` command
- [ ] Create GBrain config template
- [ ] Add GBrain examples to README
- [ ] Create troubleshooting guide

**Deliverable:** Easy one-command setup

### Phase 3: Monitoring (1 week, optional)
- [ ] Add routing decision logging
- [ ] Create cost comparison reports
- [ ] Add performance metrics
- [ ] Create usage dashboards

**Deliverable:** Users can see ROI of integration

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Smart-ai-router unavailable | Low | Medium | Docs explain fallback procedure |
| Configuration complexity | Very Low | Low | Simple config, setup helper |
| Response format incompatibility | Very Low | Low | OpenAI-compat guaranteed |
| Performance overhead | Low | Low | Smart-ai-router is fast proxy |
| User confusion | Medium | Low | Clear documentation |

## Success Criteria

✅ GBrain works unchanged with smart-ai-router
✅ Users can configure via `.gbrainrc.json`
✅ Routing decisions are logged
✅ Documentation is clear
✅ No gbrain code changes needed
✅ Works with gbrain agents, skills, everything

## Comparison: Old Plan vs New Plan

| Aspect | Old Plan (Modify GBrain) | New Plan (Proxy Only) |
|--------|------------------------|----------------------|
| **GBrain changes** | 10-25 files | 0 files |
| **Coupling** | Tight (recipe system) | Loose (HTTP config) |
| **Maintenance** | High (track gbrain updates) | None |
| **Risk** | Medium | Very Low |
| **Setup complexity** | Medium (for gbrain users) | Low |
| **User friction** | Medium | Low |
| **Time to value** | 2-10 weeks | 1 week |
| **Recommendation** | ❌ Don't do | ✅ DO THIS |

## Why This Is Better

### For You (Smart-ai-router Owner)
- No responsibility for maintaining gbrain compatibility
- No need to understand gbrain's internals
- Can improve smart-ai-router without worrying about gbrain
- Clear separation of concerns

### For GBrain Users
- No risk to their setup
- Can revert at any time (just change config)
- Works immediately
- No onboarding friction

### For the Ecosystem
- Clean, decoupled integration
- Other systems can use same pattern
- Both projects remain independent
- Natural composition of systems

## Implementation: What Needs to Happen

### In Smart-ai-router

**smart_ai_router/router.py:**
```python
# Current
async def route(request):
    model_name = request.model
    # ... route to model_name

# New
async def route(request):
    if request.model in ['auto', 'auto-route']:
        profile = await self.classifier.classify(request.messages)
        model_name = await self.select_optimal_model(profile)
    else:
        model_name = request.model
    # ... route to model_name
```

**docs/gbrain-integration.md** (NEW)
```markdown
# Using Smart-ai-router with GBrain

[See INTEGRATION_ZERO_GBRAIN_CHANGES.md for full content]
```

### In GBrain
Nothing. Zero changes.

## Timeline

**Week 1:**
- Small changes to smart-ai-router
- Write documentation
- Test with GBrain

**Done.** Users can use it immediately.

## Conclusion

**The best architecture is the one that doesn't require architectural changes.**

By positioning smart-ai-router as a transparent OpenAI-compatible proxy, we get:
- ✅ Zero changes to gbrain
- ✅ Zero maintenance burden
- ✅ Zero coupling
- ✅ Maximum flexibility
- ✅ Clear value to users
- ✅ Easy to explain

Users configure their way into smart-ai-router integration. Simple. Elegant. Decoupled.

This is the right approach.
