# Smart AI Router ↔ GBrain Integration Plan

## Executive Summary

Smart AI Router and GBrain are **highly complementary systems** that can create a powerful unified AI infrastructure. GBrain's sophisticated provider-agnostic gateway and cost optimization patterns make it an ideal host for smart-ai-router's intelligent routing engine.

### Key Synergy Points

1. **GBrain has the host infrastructure** (gateway, config, plugins, MCP)
2. **Smart-ai-router has the routing intelligence** (taxonomy, classification, cost optimization)
3. **Together they create a complete solution** for cost-optimized, capability-aware AI routing

---

## Architecture Overview: Current State

### Smart AI Router
```
smart-ai-router (Python)
├── Prompt Classifier (LLM + keyword fallback)
├── Prompt Taxonomy (domain, complexity, demands, stakes)
├── Routing Logic (cheapest model that clears quality bar)
├── Provider Abstraction (OpenRouter, Ollama, Bedrock)
└── OpenAI-compatible endpoint + Web UI
```

### GBrain
```
gbrain (TypeScript/Node.js)
├── AI Gateway (20+ providers, recipe-based config)
├── Provider Recipes (cost tracking, capability detection)
├── Skills System (80+ automated capabilities)
├── MCP Plugin Architecture (Claude, Codex plugins)
└── Evaluation Framework (BrainBench, cost optimization)
```

---

## Integration Strategy: Three Phased Approach

### Phase 1: Smart Router as a Recipe in GBrain
**Goal:** GBrain can use smart-ai-router as a provider backend

**Implementation:**
1. Create `src/core/ai/recipes/smart-router.ts` recipe
2. Configure smart-ai-router as OpenAI-compatible provider
3. Add routing metadata to the recipe
4. Integrate cost optimization from router into GBrain's budget tracker

**Benefits:**
- GBrain users get intelligent routing immediately
- smart-ai-router routing metadata surfaces in GBrain logs
- Cost optimization aligns with GBrain's existing budget system

**Integration Points:**
```typescript
// src/core/ai/recipes/smart-router.ts
export const SmartRouterRecipe: Recipe = {
  id: 'smart-router',
  displayName: 'Smart AI Router',
  type: 'native-openai-compatible',
  baseUrl: 'http://localhost:8001/v1', // or configurable URL
  capabilities: {
    chat: true,
    embedding: true,
    tools: true,
    vision: true,
    streaming: true,
  },
  routing: {
    enabled: true,
    classifies: true,           // Infers domain/complexity
    failoverChain: ['openai', 'anthropic', 'google'],
    costTracking: 'native',      // Router provides cost data
  },
  models: {
    chat: 'smart-route/auto',    // Let router decide
    embedding: 'smart-route/auto',
  },
  env: {
    SMART_ROUTER_API_KEY: 'optional',
    SMART_ROUTER_URL: 'http://localhost:8001',
  },
};
```

---

### Phase 2: Unified Routing Configuration
**Goal:** GBrain's config system manages smart-ai-router's routing rules

**Implementation:**
1. Extend `GBrainConfig` to include routing preferences
2. Sync router taxonomy (domains, complexity levels) with GBrain's skill taxonomy
3. Create config schema for routing thresholds and quality bars
4. Expose router settings in GBrain's plugin/agent interface

**Configuration Example:**
```typescript
// .gbrainrc.json (extended)
{
  "routing": {
    "enabled": true,
    "provider": "smart-router",
    "costOptimization": {
      "mode": "aggressive",      // aggressive | balanced | quality-first
      "weeklyBudget": 100,
      "fallbackTier": "anthropic"
    },
    "domains": {
      "software_engineering": {
        "qualityThreshold": 85,
        "tool_support": true,
        "maxCost": 0.01
      },
      "general": {
        "qualityThreshold": 70,
        "tool_support": false,
        "maxCost": 0.005
      }
    }
  }
}
```

**Benefits:**
- Single source of truth for routing configuration
- Declarative routing rules
- Dynamic reconfiguration without server restart

---

### Phase 3: Deep Architectural Integration
**Goal:** Merge routing intelligence into GBrain's gateway

**Implementation:**
1. Port smart-ai-router's `llm_classifier.py` logic to TypeScript
2. Integrate prompt profiling into gateway's request pipeline
3. Merge router's competence scoring with GBrain's model selection
4. Share evaluation results between systems

**New Architecture:**
```
GBrain Gateway (enhanced)
├── Request Pipeline
│   ├── Prompt Profiler (from smart-ai-router)
│   ├── Capability Matcher (GBrain + router)
│   ├── Cost Optimizer (merged logic)
│   └── Provider Selector (GBrain + router weights)
├── Model Competence Scorer (unified)
├── Cost Tracker (unified)
└── Evaluation Integration (shared evals)
```

**Benefits:**
- No separate server needed
- Lower latency
- Unified logging and observability
- Shared evaluation data

---

## Detailed Integration Points

### 1. Prompt Profiling Integration

**Current State:**
- smart-ai-router: `smart_ai_router/taxonomy.py` + `smart_ai_router/llm_classifier.py`
- GBrain: Basic model selection without domain/complexity awareness

**Integration:**
```typescript
// New: src/core/ai/profiler.ts
import { PromptProfile } from './prompt-profile';
import { PromptClassifier } from './prompt-classifier';

export class PromptProfiler {
  async profile(prompt: string): Promise<PromptProfile> {
    // Two-speed classification (triage → refine)
    // Domain, complexity, demands, stakes
    // Returns routing hints for gateway
  }
}
```

**TypeScript Port of Router's Taxonomy:**
```typescript
export interface PromptProfile {
  domains: {
    software_engineering?: number;
    law_regulatory?: number;
    medicine_health?: number;
    [key: string]: number;
  };
  complexity: 'simple' | 'moderate' | 'complex' | 'expert';
  demands: {
    toolUse: boolean;
    currentFacts: boolean;
    precision: boolean;
    creativity: boolean;
  };
  stakes: 'low' | 'medium' | 'high';
}
```

---

### 2. Competence Scoring Merger

**Current State:**
- smart-ai-router: Pattern-based competence inference
- GBrain: Manual price/capability matrix

**Unified System:**
```typescript
// src/core/ai/competence-scorer.ts
export class CompetenceScorer {
  async scoreModel(
    modelId: string,
    profile: PromptProfile
  ): Promise<CompetenceScore> {
    // Combine:
    // 1. GBrain's benchmark data
    // 2. smart-ai-router's pattern-based inference
    // 3. Evaluation history (BrainBench results)
    // 4. Domain-specific performance
    
    return {
      overall: 0.92,
      byDomain: { software_engineering: 0.95, ... },
      byDemand: { toolUse: true, precision: 0.88, ... },
    };
  }
}
```

---

### 3. Cost Optimizer Enhancement

**Current State:**
- GBrain: Basic per-token cost tracking
- smart-ai-router: Cost-aware routing with quality thresholds

**Enhanced System:**
```typescript
// src/core/ai/cost-optimizer.ts
export class CostOptimizer {
  async selectModel(
    profile: PromptProfile,
    competenceScores: Map<string, CompetenceScore>,
    budget: BudgetConstraints
  ): Promise<ModelSelection> {
    // Find cheapest model where:
    // competence >= threshold_for_complexity[profile.complexity]
    // AND total_cost <= budget
    // AND capabilities match demands
    // WITH fallback chain for quality
    
    return {
      primary: 'gpt-4-turbo',     // Cheapest qualifying
      fallback: ['claude-3-opus'], // High quality fallback
      reason: 'Meets software_engineering bar at lowest cost',
      estimatedCost: 0.003,
    };
  }
}
```

---

### 4. MCP Plugin Enhancement

**Current State:**
- GBrain exposes skills through MCP

**Enhancement:**
```typescript
// .codex-plugin/mcp.json (extended)
{
  "resources": {
    "routing/config": "Read/write routing preferences",
    "routing/stats": "Cost and routing statistics",
    "routing/domains": "List profiled domains",
    "routing/models": "List available models with competence scores"
  },
  "tools": {
    "route/analyze": "Profile a prompt and show routing decision",
    "route/compare": "Compare cost of routing vs direct API call",
    "route/override": "Override routing for a single request",
    "route/audit": "Audit routing decisions over time"
  }
}
```

---

### 5. Evaluation Integration

**Current State:**
- smart-ai-router: Manual competence priors
- GBrain: BrainBench, LongMemEval, domain-specific evals

**Integration:**
```typescript
// src/core/ai/evaluation-sync.ts
export class EvaluationSync {
  // Pull GBrain eval results → update smart-router competence priors
  // Pull router routing decisions → feed into GBrain's SkillOpt
  // Cross-validate: Does high competence score = good outcomes?
  
  async syncEvaluationData(): Promise<void> {
    const brainBenchResults = await this.loadBrainBenchResults();
    const routingHistory = await this.loadRoutingHistory();
    
    // Update competence priors
    await this.updateCompetencePriors(brainBenchResults, routingHistory);
  }
}
```

---

## Implementation Roadmap

### Week 1-2: Phase 1 (Recipe Integration)
- [ ] Create `src/core/ai/recipes/smart-router.ts`
- [ ] Test smart-router recipe in GBrain
- [ ] Document configuration
- [ ] Create e2e test

**Deliverable:** GBrain users can select `smart-router` as provider

### Week 3-4: Phase 2 (Config Integration)
- [ ] Extend `GBrainConfig` with routing section
- [ ] Implement config validation
- [ ] Create config UI/wizard
- [ ] Sync router taxonomy with skills

**Deliverable:** Config file can express routing preferences

### Week 5-8: Phase 3 (Architecture Integration)
- [ ] Port `llm_classifier.py` → TypeScript
- [ ] Integrate prompt profiler into gateway
- [ ] Merge competence scorers
- [ ] Implement cost optimizer
- [ ] Update MCP plugin

**Deliverable:** Unified routing in GBrain gateway

### Week 9-10: Testing & Docs
- [ ] Benchmark routing decisions (cost, quality)
- [ ] Evaluation framework integration
- [ ] Docs: architecture, config, troubleshooting
- [ ] Migration guide for existing users

**Deliverable:** Production-ready integration

---

## File Structure After Integration

```
gbrain/
├── src/core/ai/
│   ├── gateway.ts (enhanced with prompt profiling)
│   ├── recipes/
│   │   ├── smart-router.ts (new)
│   │   └── ... (existing recipes)
│   ├── prompt-profile.ts (new)
│   ├── prompt-classifier.ts (ported from Python)
│   ├── competence-scorer.ts (merged logic)
│   ├── cost-optimizer.ts (enhanced)
│   └── evaluation-sync.ts (new)
├── .codex-plugin/
│   └── mcp.json (enhanced with routing tools)
├── evals/
│   └── routing-eval.ts (new - routing quality benchmark)
├── src/config/
│   └── gbrainconfig.ts (extended with routing section)
└── docs/
    └── ROUTING_INTEGRATION.md (new)
```

---

## API Compatibility

### OpenAI-Compatible Endpoint
Both systems maintain OpenAI compatibility:
- smart-ai-router: POST `/v1/chat/completions`
- GBrain gateway: (will also expose this)

### Configuration Formats
**smart-ai-router:**
```bash
smart-ai-router setup
SMART_ROUTER_API_KEY=...
SMART_ROUTER_OPENROUTER_KEY=...
```

**GBrain extended:**
```json
{
  "routing": {
    "provider": "smart-router",
    "apiKey": "...",
    "url": "http://localhost:8001/v1"
  }
}
```

---

## Benefits & Outcomes

### For smart-ai-router Users
- Access to GBrain's sophisticated evaluation framework
- Skills-based automation integration
- MCP plugin ecosystem
- Unified TypeScript/Node codebase (optional native port)
- Better integration with Cursor/Claude workflows

### For GBrain Users
- Intelligent cost-optimized routing out of the box
- Prompt profiling and domain classification
- Quality thresholds and competence scoring
- Better cost control and budgeting
- Fallback chain management

### For Both Projects
- Shared evaluation data and benchmarks
- Unified observability and logging
- Community of cost-conscious AI practitioners
- Best practices for vendor-agnostic routing
- Production-tested patterns

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| **Complexity of porting Python → TypeScript** | Start with Phase 1 (recipe), Phase 3 can wait or use Python subprocess |
| **GBrain architecture changes** | Recipe approach (Phase 1) is loosely coupled; can coexist independently |
| **Smart-router requires Python/macOS LaunchAgent** | Keep as optional service; GBrain can call via network API |
| **Evaluation data quality** | Start with BrainBench results; gradually add domain-specific evals |
| **Config schema conflicts** | Use namespaced config section (`routing:`) to avoid collisions |

---

## Next Steps

### Immediate (This Week)
1. **Get feedback** on integration strategy from both projects
2. **Validate** gbrain's willingness to adopt smart-router patterns
3. **Scope** Phase 1 effort (recipe creation)

### Short Term (Weeks 2-4)
1. Create working smart-router recipe for GBrain
2. Document integration approach
3. Implement config schema extension
4. Create pilot use case (e.g., GBrain skill that uses smart routing)

### Medium Term (Weeks 5-10)
1. Port Python classifier to TypeScript
2. Integrate into GBrain gateway
3. Merge competence and cost logic
4. Update MCP plugin interface

---

## Questions for Stakeholders

1. **Garry Tan (GBrain):** Would you be interested in adopting smart-router's routing patterns?
2. **GBrain Community:** What's the biggest pain point in cost optimization today?
3. **smart-ai-router Users:** What additional capabilities would make routing better?

---

## Conclusion

Smart AI Router and GBrain are natural partners. The integration can happen in phases, with Phase 1 providing immediate value with minimal risk. The unified system would be a powerful foundation for cost-aware, capability-aware AI infrastructure.

**Recommended first step:** Implement Phase 1 (Smart Router Recipe) to validate the approach.
