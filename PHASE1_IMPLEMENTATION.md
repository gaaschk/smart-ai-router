# Phase 1: Smart Router Recipe for GBrain
## Practical Implementation Guide

### Overview
Phase 1 makes smart-ai-router available as a provider recipe in GBrain. This is a 1-2 week effort requiring minimal changes to both codebases and providing immediate value.

### What Gets Built
1. A recipe file that GBrain can use to route through smart-ai-router
2. Configuration to expose smart-ai-router's routing metadata
3. Integration with GBrain's cost tracking
4. Documentation and examples

### Architecture Diagram
```
GBrain App
    │
    ├─→ AI Gateway
    │       │
    │       ├─→ Recipe Selector
    │       │       │
    │       │       └─→ smart-router recipe ◄── NEW
    │       │
    │       └─→ HTTP Client
    │           (OpenAI-compatible)
    │               │
    │               └─→ smart-ai-router service
    │                   (running on localhost:8001)
    │
    └─→ Cost Tracker
            │
            └─→ Reads routing cost from response headers
```

---

## Step 1: Create the Smart Router Recipe

**File:** `gbrain/src/core/ai/recipes/smart-router.ts`

```typescript
import { Recipe, ModelCapabilities, ProviderType } from '../types';

export const smartRouterRecipe: Recipe = {
  id: 'smart-router',
  displayName: 'Smart AI Router',
  description: 'Intelligent cost-optimized routing via smart-ai-router service',
  type: 'native-openai-compatible' as ProviderType,
  
  // Connection
  baseUrl: () => process.env.SMART_ROUTER_URL || 'http://localhost:8001/v1',
  apiKeyEnvVar: 'SMART_ROUTER_API_KEY', // optional
  
  // Capabilities
  capabilities: {
    chat: true,
    embedding: false, // smart-router focuses on chat routing
    vision: true,
    tools: true,
    streaming: true,
    functions: true,
  } as ModelCapabilities,
  
  // Model selection
  models: {
    chat: 'smart-route/auto', // Use smart-router's default routing
  },
  
  // Cost tracking
  costPerToken: {
    // smart-router will return actual cost in response headers
    // Format: X-Actual-Cost, X-Model-Used, X-Routing-Decision
    // GBrain will parse these and update cost tracker
    input: 0.0, // placeholder - overridden by header
    output: 0.0,
  },
  
  // Configuration
  config: {
    maxContextLength: 100000,
    requestTimeoutMs: 120000,
  },
  
  // Environment variables
  env: {
    SMART_ROUTER_URL: {
      description: 'URL to smart-ai-router service',
      default: 'http://localhost:8001/v1',
      required: false,
    },
    SMART_ROUTER_API_KEY: {
      description: 'API key for smart-ai-router (if configured)',
      required: false,
    },
  },
  
  // Health check
  healthCheck: async (client: any) => {
    try {
      const baseUrl = process.env.SMART_ROUTER_URL || 'http://localhost:8001/v1';
      const response = await fetch(`${baseUrl.replace('/v1', '')}/health`);
      return response.ok;
    } catch {
      return false;
    }
  },
  
  // Request interceptor - add routing metadata to headers
  beforeRequest: async (request: any) => {
    // Optional: GBrain can pass hint about desired optimization mode
    request.headers['X-Routing-Mode'] = 
      process.env.SMART_ROUTER_MODE || 'balanced'; // aggressive|balanced|quality-first
    
    return request;
  },
  
  // Response interceptor - parse routing metadata
  afterResponse: async (response: any) => {
    // Extract routing decision metadata from response headers
    const metadata = {
      modelUsed: response.headers['x-model-used'],
      routingDecision: response.headers['x-routing-decision'],
      actualCost: parseFloat(response.headers['x-actual-cost'] || '0'),
      escalated: response.headers['x-escalated'] === 'true',
      domainProfile: response.headers['x-domain-profile'],
    };
    
    // Store routing metadata in response for GBrain's cost tracker
    response.routingMetadata = metadata;
    
    // Update cost if header provided
    if (metadata.actualCost > 0) {
      response.costOverride = {
        inputCost: 0, // router provides total
        outputCost: metadata.actualCost,
      };
    }
    
    return response;
  },
};

export default smartRouterRecipe;
```

---

## Step 2: Enhance Smart-Router's Response Headers

**File:** `smart_ai_router/api/proxy.py`

Add these headers to every response so GBrain can extract routing data:

```python
# In the proxy's response handler (after routing decision is made)

async def stream_response(response):
    """Stream response with routing metadata in headers"""
    
    # Collect routing metadata
    routing_metadata = {
        'X-Model-Used': selected_model.name,
        'X-Routing-Decision': f'Selected {selected_model.name} for domain={profile.primary_domain}, complexity={profile.complexity}',
        'X-Actual-Cost': str(estimated_cost),
        'X-Escalated': str(was_escalated),
        'X-Domain-Profile': json.dumps({
            'domains': profile.domains,
            'complexity': profile.complexity,
            'demands': profile.demands,
        }),
    }
    
    # Include headers in streaming response
    async for chunk in provider_response:
        if first_chunk:
            # Send headers with first chunk
            headers = {**streaming_headers, **routing_metadata}
            yield format_sse_headers(headers)
            first_chunk = False
        
        yield chunk
```

---

## Step 3: GBrain Gateway Integration

**File:** `gbrain/src/core/ai/gateway.ts`

```typescript
// In the gateway's provider initialization:

import { smartRouterRecipe } from './recipes/smart-router';

// Register the smart router recipe
export const RECIPES = {
  // ... existing recipes ...
  'smart-router': smartRouterRecipe,
};

// In the gateway's response handling:

export async function handleProviderResponse(
  response: Response,
  recipe: Recipe
): Promise<void> {
  
  if (recipe.id === 'smart-router') {
    // Extract routing metadata from headers
    const modelUsed = response.headers.get('x-model-used');
    const actualCost = parseFloat(
      response.headers.get('x-actual-cost') || '0'
    );
    const routingDecision = response.headers.get('x-routing-decision');
    const domainProfile = response.headers.get('x-domain-profile');
    
    // Log routing decision for observability
    console.log('Routing via smart-ai-router:', {
      modelUsed,
      actualCost,
      routingDecision,
      domainProfile,
    });
    
    // Update cost tracker with actual cost
    if (actualCost > 0) {
      costTracker.addCost(actualCost, {
        provider: 'smart-router',
        model: modelUsed,
        metadata: { routingDecision },
      });
    }
  }
  
  // ... existing response handling ...
}
```

---

## Step 4: Configuration Integration

**File:** `gbrain/.gbrainrc.example.json`

```json
{
  "provider": "smart-router",
  "smartRouter": {
    "enabled": true,
    "url": "http://localhost:8001/v1",
    "apiKey": "optional-api-key",
    "routingMode": "balanced"
  }
}
```

---

## Step 5: Documentation

**File:** `gbrain/docs/SMART_ROUTER_INTEGRATION.md`

```markdown
# Smart AI Router Integration

GBrain can route all LLM requests through smart-ai-router for intelligent 
cost optimization.

## Setup

### 1. Install and run smart-ai-router

\`\`\`bash
git clone https://github.com/gaaschk/smart-ai-router.git
cd smart-ai-router
pip install -e .
smart-ai-router setup
\`\`\`

This starts the router on http://localhost:8001

### 2. Configure GBrain

In your `.gbrainrc.json`:

\`\`\`json
{
  "provider": "smart-router"
}
\`\`\`

### 3. That's it!

When you use GBrain, all requests will be routed through smart-ai-router.

## How It Works

For each prompt, smart-ai-router:

1. **Classifies** the prompt (domain, complexity, demands)
2. **Selects** the cheapest model that meets quality requirements
3. **Falls back** to higher-quality models only if needed
4. **Returns** routing metadata in headers

GBrain's cost tracker automatically captures the actual cost and 
routing decision for each request.

## Monitoring

View routing decisions in GBrain's logs:

\`\`\`bash
smart-ai-router logs  # Show smart-router logs
\`\`\`

## Troubleshooting

**"Connection refused"**: Make sure smart-ai-router is running
\`\`\`bash
curl http://localhost:8001/health
\`\`\`

**"Wrong models being selected"**: Check smart-router's logs
\`\`\`bash
smart-ai-router logs --tail=50
\`\`\`

## Configuration

Set routing mode in `.gbrainrc.json`:

\`\`\`json
{
  "smartRouter": {
    "routingMode": "aggressive"  // aggressive | balanced | quality-first
  }
}
\`\`\`
```

---

## Step 6: Testing

**File:** `gbrain/tests/recipes/smart-router.test.ts`

```typescript
import { describe, it, expect, beforeAll, afterAll } from '@jest/globals';
import { smartRouterRecipe } from '@/core/ai/recipes/smart-router';
import { createTestClient } from '@/test-utils';

describe('Smart Router Recipe', () => {
  let testClient: any;
  
  beforeAll(async () => {
    // Start a mock smart-router service
    testClient = await createTestClient();
  });
  
  afterAll(async () => {
    await testClient.close();
  });
  
  it('should initialize from env vars', () => {
    process.env.SMART_ROUTER_URL = 'http://localhost:8001/v1';
    const recipe = smartRouterRecipe;
    expect(recipe.baseUrl()).toBe('http://localhost:8001/v1');
  });
  
  it('should extract routing metadata from response headers', async () => {
    const response = new Response('{"result": "test"}', {
      headers: {
        'x-model-used': 'gpt-4-turbo',
        'x-actual-cost': '0.003',
        'x-routing-decision': 'Selected gpt-4-turbo for cost optimization',
      },
    });
    
    const enhanced = await recipe.afterResponse(response);
    expect(enhanced.routingMetadata.modelUsed).toBe('gpt-4-turbo');
    expect(enhanced.routingMetadata.actualCost).toBe(0.003);
  });
  
  it('should handle missing routing headers gracefully', async () => {
    const response = new Response('{"result": "test"}', {
      headers: {},
    });
    
    const enhanced = await recipe.afterResponse(response);
    expect(enhanced.routingMetadata.modelUsed).toBeUndefined();
    expect(enhanced.routingMetadata.actualCost).toBe(0);
  });
});
```

---

## Step 7: CLI Integration (Optional)

**File:** `gbrain/src/cli/commands/config.ts`

Add a config command to set up smart-router:

```bash
$ gbrain config provider smart-router
✓ Smart Router configuration

? Smart Router URL: (http://localhost:8001/v1)
? Routing mode: (balanced) [aggressive|balanced|quality-first]
? API Key (optional): 

Checking smart-ai-router health... ✓ Running
Saving configuration... ✓

Your GBrain is now routing through smart-ai-router!
```

---

## Deployment Checklist

- [ ] Smart-router recipe created and tested
- [ ] Response headers added to smart-ai-router
- [ ] GBrain gateway integration done
- [ ] Configuration options documented
- [ ] Tests passing
- [ ] Example `.gbrainrc.json` updated
- [ ] Documentation written
- [ ] Health check working
- [ ] Cost tracking verified
- [ ] Code review complete

---

## Success Criteria

✅ GBrain can route requests through smart-ai-router
✅ Routing decisions are visible in logs
✅ Cost tracking captures actual costs from router
✅ Health check detects if smart-router is down
✅ Configuration is simple (one line in `.gbrainrc.json`)
✅ Documentation is clear for new users
✅ No breaking changes to existing GBrain functionality

---

## Timeline

**Week 1:**
- Day 1: Create recipe file
- Day 2: Add response headers to smart-router
- Day 3: Integrate with GBrain gateway
- Day 4: Write tests
- Day 5: Documentation

**Week 2:**
- Day 1-3: Code review and iteration
- Day 4: Deploy to staging
- Day 5: Final testing and merge

---

## Next Steps After Phase 1

Once Phase 1 is working:

- **Phase 2:** Extend `.gbrainrc.json` with full routing configuration
- **Phase 3:** Port classifier to TypeScript and integrate into gateway
- **Phase 4:** Expose routing controls via MCP plugin

Phase 1 stands alone and provides value immediately.
