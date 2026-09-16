# Smart AI Router ↔ GBrain Integration: ZERO Changes to GBrain

## The Revised Approach

Since you don't own gbrain and want to avoid modifying it, the integration strategy changes completely. Instead of making gbrain dependent on smart-ai-router, **smart-ai-router becomes a complete standalone proxy that gbrain uses as just another OpenAI-compatible provider**.

## Key Insight

GBrain already supports **OpenAI-compatible providers**. Smart-ai-router is already OpenAI-compatible. Therefore:

✅ **No changes needed to gbrain whatsoever**
✅ **Smart-ai-router works out of the box with gbrain**
✅ **All intelligence stays in smart-ai-router**
✅ **GBrain treats smart-ai-router like any other API endpoint**

## How It Works: The Simple Path

### Current State
```
GBrain → OpenRouter/Ollama/Bedrock
         (direct provider calls)
```

### With Smart-ai-router (No GBrain Changes)
```
GBrain → Smart-ai-router (intelligent router)
         ├→ OpenRouter
         ├→ Ollama  
         └→ Bedrock
```

## Setup: What Users Do

### Step 1: Start Smart-ai-router
```bash
# User runs this once
git clone https://github.com/gaaschk/smart-ai-router.git
cd smart-ai-router
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
smart-ai-router setup
# Router now runs on http://localhost:8001
```

### Step 2: Configure GBrain to Use Smart-ai-router
**User's existing `.gbrainrc.json`:**
```json
{
  "engine": "pglite",
  "chat_model": "openai:gpt-4-turbo"
}
```

**User changes it to:**
```json
{
  "engine": "pglite",
  "chat_model": "openai:gpt-4-turbo",
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```

**That's it.** GBrain now routes through smart-ai-router.

### Step 3: Optional - Use Smart-ai-router's Special Model ID
Users can optionally use smart-ai-router's special routing:
```json
{
  "chat_model": "openai:auto-route"
}
```

This tells smart-ai-router to pick the best model automatically.

## What Changes in Smart-ai-router

To make this work, smart-ai-router needs:

### 1. Support for Custom Model IDs (SMALL CHANGE)
**File:** `smart_ai_router/router.py`

Currently smart-ai-router expects real model names. It should accept a special model ID:

```python
# In the routing logic
if model_id == "auto-route" or model_id == "auto":
    # Use smart-router's full intelligence
    selected_model = await self.smart_route(profile)
else:
    # User specified a model, route to it directly
    selected_model = await self.find_model(model_id)
```

### 2. Ensure Response Headers are Returned (ALREADY DONE)
Response headers with routing metadata should be returned so users can see what happened:
```
X-Model-Used: gpt-4-turbo
X-Actual-Cost: 0.003
X-Routing-Decision: Selected gpt-4-turbo for software_engineering
```

### 3. Add OpenAI Compatibility for Custom Fields (SMALL CHANGE)
Some users might want to request a specific optimization mode:
```python
# Accept optional header from client
routing_mode = request.headers.get('X-Routing-Mode', 'balanced')
# Possible values: aggressive, balanced, quality-first
```

## GBrain Integration: Completely Passive

GBrain doesn't need to know about smart-ai-router at all. It sees it as just another OpenAI endpoint:

1. User points GBrain's `provider_base_urls.openai` to smart-ai-router
2. GBrain sends requests to smart-ai-router's `/v1/chat/completions`
3. Smart-ai-router routes internally and returns OpenAI-compatible response
4. GBrain records the response cost using its normal OpenAI pricing
5. **Optional:** GBrain could parse `X-Actual-Cost` header if it wanted to (but doesn't have to)

## What Users Can Do

### Configuration Approach (No Code)
Users configure smart-ai-router by editing `.gbrainrc.json`:
```json
{
  "provider_base_urls": {
    "openai": "http://localhost:8001/v1"
  }
}
```

### Command-Line Approach (Optional)
Smart-ai-router could expose a CLI to add GBrain integration:
```bash
smart-ai-router configure-for-gbrain
# Interactive setup that:
# 1. Tests the connection
# 2. Shows what to add to .gbrainrc.json
# 3. Optionally modifies user's GBrain config
```

### Environment Variable Approach
Users set:
```bash
OPENAI_BASE_URL=http://localhost:8001/v1
```

And GBrain automatically uses it.

## Architecture Diagram

```
User's GBrain Installation
├── .gbrainrc.json
│   └── provider_base_urls.openai = http://localhost:8001/v1
├── Skills
├── Agents
└── Conversations

         ↓ (HTTP calls to)

User's Smart-ai-router Installation
├── smart_ai_router/
│   ├── router.py (routing logic)
│   ├── classifier.py (prompt profiling)
│   └── api/proxy.py (OpenAI endpoint)
└── Listens on http://localhost:8001/v1
    (drop-in replacement for OpenAI API)
    
         ↓ (makes calls to)
         
Provider APIs
├── OpenRouter
├── Ollama
└── AWS Bedrock
```

## Changes Required: MINIMAL

### Smart-ai-router Changes
**Files to modify:** 2-3 files
**Lines of code:** ~50 lines
**Risk:** Very low (additive features)
**Effort:** 2-4 hours

1. **router.py:** Accept `auto-route` model ID (~15 lines)
2. **api/proxy.py:** Already returns response headers (~0 lines, already done)
3. **docs/integration-gbrain.md:** New file explaining setup (~100 lines)

### GBrain Changes
**Files to modify:** 0
**Risk:** None
**Effort:** 0 hours

Users just configure it. No code changes needed.

## Documentation Needed

### For Smart-ai-router
**New file:** `docs/gbrain-integration.md`
```markdown
# Using Smart-ai-router with GBrain

## Setup

1. Start smart-ai-router normally
2. In your GBrain `.gbrainrc.json`, add:
   
   {
     "provider_base_urls": {
       "openai": "http://localhost:8001/v1"
     }
   }

3. GBrain will now route through smart-ai-router

## How It Works

GBrain thinks it's calling OpenAI, but:
- Requests actually go to smart-ai-router
- Smart-ai-router classifies the prompt
- Smart-ai-router routes to the cheapest qualifying model
- Response comes back to GBrain as if from OpenAI

## Monitoring

To see routing decisions:
```bash
smart-ai-router logs
```

## Configuration

To set routing mode in smart-ai-router:
```bash
smart-ai-router config --routing-mode aggressive
```

This affects all clients (including GBrain).
```

### For Users
Update smart-ai-router README with GBrain section:
```markdown
## Using with GBrain

GBrain is a sophisticated agent brain that supports custom OpenAI-compatible endpoints.

You can use smart-ai-router as GBrain's routing engine:

1. Start smart-ai-router
2. Configure GBrain to use it
3. All your agent prompts get cost-optimized routing

See [gbrain-integration.md](docs/gbrain-integration.md) for details.
```

## Why This Approach is Better

### ✅ Zero Risk to GBrain
- GBrain code unchanged
- Works with any version of GBrain
- Can be removed anytime by changing config

### ✅ Zero Maintenance Burden
- No need to track gbrain updates
- No need to maintain compatibility
- gbrain changes don't break anything

### ✅ Zero Coupling
- Smart-ai-router works independently
- GBrain doesn't know it exists
- Can use smart-ai-router elsewhere too

### ✅ Zero Dependencies
- GBrain doesn't depend on smart-ai-router
- Smart-ai-router doesn't depend on gbrain
- Completely decoupled systems

### ✅ Zero Complexity
- Simple HTTP proxy relationship
- Follows standard OpenAI API
- Works with any client using OpenAI-compatible endpoints

### ✅ Clear Value Prop
- Users get cost optimization
- GBrain users benefit immediately
- No changes to their workflow

## The Real Integration Point

The integration isn't at the code level—it's at the **configuration level**:

```
GBrain Config (user edits) → Smart-ai-router → Provider APIs
```

That's it. Simple, elegant, decoupled.

## Implementation for Smart-ai-router

### Change 1: Accept `auto-route` Model ID
**File:** `smart_ai_router/router.py`

```python
async def route(self, request):
    profile = await self.classifier.classify(request.messages)
    
    # NEW: Check for special routing request
    if request.model in ['auto', 'auto-route']:
        # Use smart-router to pick the best model
        model_name = await self.select_optimal_model(profile)
    else:
        # Use the requested model
        model_name = request.model
    
    # Route to selected model...
```

### Change 2: Ensure Header Propagation (Already Done)
Response headers already include routing metadata. Nothing to change.

### Change 3: Documentation
Add `docs/gbrain-integration.md` with setup instructions.

## Timeline

- **Week 1:** Make 2-3 small changes to smart-ai-router, write documentation
- **No weeks needed for gbrain changes** (none required)
- **Done:** Users can immediately use smart-ai-router with gbrain

## What Users See

### Before
GBrain sends all requests directly to OpenAI, Anthropic, etc.

### After
GBrain sends requests to smart-ai-router, which:
1. Classifies the prompt
2. Evaluates available models
3. Routes to the cheapest one that meets quality bar
4. Returns response with routing metadata

**Result:** 60%+ cost savings, transparent to GBrain

## Edge Cases

### Q: What if smart-ai-router goes down?
A: GBrain will fail with connection error. User can either:
- Restart smart-ai-router
- Change config back to direct provider
- Point to a backup smart-ai-router instance

### Q: Can users see what model was selected?
A: Yes! Smart-ai-router returns headers:
```
X-Model-Used: gpt-4-turbo
X-Routing-Decision: Selected for cost optimization
```

GBrain won't display these by default, but advanced users can check logs.

### Q: Does this work with GBrain's agent mode?
A: Yes! All GBrain features work through smart-ai-router since it's a transparent proxy.

### Q: Can this work offline?
A: Yes, if using Ollama. Smart-ai-router can route to local Ollama models.

## Success Criteria

✅ Smart-ai-router accepts `auto-route` model ID
✅ Response headers include routing metadata
✅ Documentation explains GBrain setup
✅ Users can point GBrain to smart-ai-router via config
✅ GBrain works unchanged with smart-ai-router
✅ Cost savings visible in smart-ai-router logs

## Conclusion

**The best integration is one that requires no changes to the integrated system.**

By treating smart-ai-router as just another OpenAI-compatible endpoint, we get:
- ✅ Clean separation of concerns
- ✅ Zero maintenance burden
- ✅ Zero risk to gbrain
- ✅ Maximum flexibility for users
- ✅ Works with any OpenAI-compatible system

Users get intelligent cost-optimized routing without any code changes to gbrain.

**This is the right approach.**
