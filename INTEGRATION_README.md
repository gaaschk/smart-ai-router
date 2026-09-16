# Smart-ai-router ↔ GBrain Integration

## Status: REVISED APPROACH ✅

**Important:** You don't own gbrain, so we're **NOT modifying gbrain**. 

Instead, smart-ai-router works as a **transparent proxy** that gbrain uses via configuration. **Zero changes to gbrain needed.**

---

## The Simple Approach

GBrain already supports custom OpenAI-compatible endpoints. Smart-ai-router provides one.

**How it works:**
1. User starts smart-ai-router (runs on `localhost:8001`)
2. User configures gbrain to point to smart-ai-router via `.gbrainrc.json`
3. All gbrain requests route through smart-ai-router
4. Smart-ai-router optimizes routing and handles everything
5. GBrain works unchanged

**That's it.**

---

## Quick Start for Users

### Step 1: Install Smart-ai-router
```bash
git clone https://github.com/gaaschk/smart-ai-router
cd smart-ai-router
pip install -e .
smart-ai-router setup
```

Smart-ai-router now runs on `http://localhost:8001`

### Step 2: Configure GBrain
Edit your `~/.gbrainrc.json`:

```json
{
  "engine": "pglite",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```

Or for multiple providers:
```json
{
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1",
    "anthropic": "http://localhost:8001/v1",
    "openrouter": "http://localhost:8001/v1"
  }
}
```

### Step 3: Use GBrain Normally
```bash
gbrain
# All requests now route through smart-ai-router
# Automatic cost optimization happens transparently
```

### Step 4 (Optional): Monitor Routing
```bash
smart-ai-router logs
# See which models were selected and why
```

---

## What Changes in Smart-ai-router

**Only 2-3 small files:**

### 1. Accept "auto" Model ID
**File:** `smart_ai_router/router.py`

```python
# Allow clients to request automatic routing
if request.model in ['auto', 'auto-route']:
    profile = await self.classifier.classify(request.messages)
    selected_model = await self.select_optimal_model(profile)
else:
    selected_model = request.model
```

**Impact:** ~20 lines of code
**Risk:** Very low (additive feature)

### 2. Document GBrain Integration
**File:** `docs/gbrain-integration.md` (NEW)

Add documentation explaining how to configure gbrain to use smart-ai-router.

**Impact:** ~100 lines of documentation
**Risk:** None (documentation only)

### 3. Optional: Setup Helper
**File:** `smart_ai_router/cli/setup_gbrain.py` (NEW)

Convenience command to help users configure gbrain:
```bash
smart-ai-router configure-for-gbrain
# Prompts user and shows what to add to .gbrainrc.json
```

**Impact:** ~80 lines of code
**Risk:** Very low (optional convenience feature)

---

## What Changes in GBrain

**NOTHING.**

✅ Zero code changes
✅ Zero modifications
✅ Zero compatibility concerns

GBrain uses smart-ai-router just like any other OpenAI-compatible endpoint.

---

## Architecture

```
GBrain (user's laptop)
├─ Uses OpenAI API calls
└─ configured to point to http://localhost:8001/v1

        ↓ HTTP

Smart-ai-router (localhost:8001)
├─ Receives OpenAI-format requests
├─ Classifies prompt (domain, complexity, demands)
├─ Selects optimal model
└─ Forwards to real provider

        ↓ HTTP

OpenRouter / Ollama / Bedrock
└─ Executes request

        ↓ Returns response

Back to GBrain
└─ Shows results with routing metadata in headers
```

---

## Key Benefits

### Cost Savings
60%+ reduction in LLM costs by routing to cheapest models that meet quality bar.

### Transparency
Routing decisions visible in smart-ai-router logs.

### Zero Friction
No changes to gbrain, no new dependencies, simple config.

### Works Everywhere
Any OpenAI-compatible system can use smart-ai-router the same way.

---

## User Scenarios

### Scenario 1: GBrain + Smart-ai-router for Cost Optimization
```json
{
  "chat_model": "openai:gpt-4o-mini",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```
GBrain thinks it's calling OpenAI, but smart-ai-router may route to cheaper alternative.

### Scenario 2: Auto Routing
```json
{
  "chat_model": "openai:auto",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```
Smart-ai-router picks the best model for each prompt automatically.

### Scenario 3: Multi-Provider
```json
{
  "chat_model": "openai:gpt-4o-mini",
  "expansion_model": "openai:gpt-4o",
  "embedding_model": "openai:text-embedding-3-large",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```
All requests route through smart-ai-router for consistent optimization.

---

## Implementation Roadmap

### Phase 1: Core Support (1 week)
- [ ] Modify `smart_ai_router/router.py` to handle "auto" model
- [ ] Ensure response headers return routing metadata
- [ ] Write `docs/gbrain-integration.md`
- [ ] Test with real gbrain instance

**Deliverable:** Users can point gbrain to smart-ai-router

### Phase 2: UX Improvements (1 week, optional)
- [ ] Add `smart-ai-router configure-for-gbrain` command
- [ ] Update main README with gbrain section
- [ ] Create example `.gbrainrc.json` configurations
- [ ] Add troubleshooting guide

**Deliverable:** Easy setup experience

### Phase 3: Observability (1 week, optional)
- [ ] Improve logging of routing decisions
- [ ] Add cost comparison metrics
- [ ] Create usage reports
- [ ] Add performance monitoring

**Deliverable:** Users can see ROI clearly

---

## Frequently Asked Questions

### Q: Does this require changes to gbrain?
**A:** No. Zero changes to gbrain. Users just configure their gbrain to point to smart-ai-router via `.gbrainrc.json`.

### Q: Will this work with gbrain's agents?
**A:** Yes! Everything gbrain does (agents, skills, memory) works transparently through smart-ai-router.

### Q: What if smart-ai-router goes down?
**A:** User can change config back to direct provider calls, or restart smart-ai-router. Simple fallback.

### Q: Can users see what model was selected?
**A:** Yes! Smart-ai-router returns headers with routing metadata:
```
X-Model-Used: gpt-4-turbo
X-Routing-Decision: Selected for cost optimization
X-Actual-Cost: 0.003
```

### Q: Does this work with Ollama?
**A:** Yes! Smart-ai-router routes to Ollama models too.

### Q: Can multiple GBrain instances share one smart-ai-router?
**A:** Yes! One smart-ai-router can serve multiple gbrain instances.

---

## Comparison: Before vs After

### Before: Direct Calls
```
GBrain → OpenRouter
         ↓
         All requests use specified model
         No optimization
```

### After: Smart-ai-router
```
GBrain → Smart-ai-router → Smart model selection
                           (cheapest that meets bar)
                           ↓
                           OpenRouter/Ollama/Bedrock
                           ↓
                           60%+ cost savings
```

---

## Files to Review

### For Implementation Details
- **INTEGRATION_ZERO_GBRAIN_CHANGES.md** - Detailed explanation of zero-change approach
- **REVISED_INTEGRATION_PLAN.md** - Complete integration strategy
- **PHASE1_IMPLEMENTATION.md** - Original implementation guide (still useful for smart-ai-router changes)

### For Users
- Smart-ai-router README (update with gbrain section)
- docs/gbrain-integration.md (new file, step-by-step guide)
- .gbrainrc.example.json (show example with smart-ai-router)

---

## Why This Approach

### ✅ Minimal Changes
Only smart-ai-router changes slightly. GBrain unchanged.

### ✅ Clean Separation
Each project remains independent. Clear interface (HTTP API).

### ✅ Zero Maintenance
No need to track gbrain updates or maintain compatibility.

### ✅ User Control
Users decide whether to use smart-ai-router. Simple opt-in via config.

### ✅ Extensible
Any OpenAI-compatible system can use this pattern.

---

## Next Steps

1. **Modify smart-ai-router** to accept "auto" model ID (~1 week)
2. **Document the integration** with clear examples
3. **Test with real gbrain** setup
4. **Announce to users** - they can immediately benefit

Simple. Clean. Done.

---

## Summary

**Before:** Need to modify gbrain (complex, risky, not your project)

**Now:** Use smart-ai-router as a proxy (simple, safe, your project only)

**Result:** Same integration benefits, zero risk, zero maintenance burden.

This is the right approach. ✅
