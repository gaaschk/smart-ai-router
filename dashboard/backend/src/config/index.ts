import dotenv from 'dotenv';

dotenv.config();

export const config = {
  // Server
  nodeEnv: process.env.NODE_ENV || 'development',
  // 5000 collides with macOS's AirPlay Receiver / ControlCenter (which binds it
  // by default on modern Macs, including the Mac Mini this is deployed to), so
  // the default here is 5050 instead. Override with PORT if that's still busy.
  port: parseInt(process.env.PORT || '5050', 10),
  logLevel: process.env.LOG_LEVEL || 'info',

  // Database
  database: {
    url: process.env.DATABASE_URL || 'postgresql://postgres:postgres@localhost:5432/dashboard',
  },

  // Smart AI Router
  smartRouter: {
    url: process.env.SMART_ROUTER_URL || 'http://localhost:8001',
    apiKey: process.env.SMART_ROUTER_API_KEY || '',
    timeout: 30000, // 30 seconds
  },

  // GBrain
  //
  // GBrain has no HTTP server — it's a CLI (`gbrain call <tool> '<json>'`) plus
  // a stdio-only MCP server (`gbrain serve`), neither of which a Node backend
  // can reach over HTTP. We shell out to the CLI instead (see gbrainClient.ts).
  // Its default PGLite store also takes an exclusive file lock, so calls must
  // be serialized — one gbrain process at a time — or they time out waiting
  // on the lock.
  gbrain: {
    bin: process.env.GBRAIN_BIN || 'gbrain',
    databaseUrl: process.env.GBRAIN_DATABASE_URL || '',
    cliTimeoutMs: parseInt(process.env.GBRAIN_CLI_TIMEOUT_MS || '30000', 10),
  },

  // Authentication
  auth: {
    jwtSecret: process.env.JWT_SECRET || 'dev-secret-key-change-me',
    sessionTimeout: parseInt(process.env.SESSION_TIMEOUT || '3600000', 10),
  },

  // CORS
  cors: {
    origin: process.env.CORS_ORIGIN || 'http://localhost:5173',
  },

  // WebSocket
  websocket: {
    pingInterval: parseInt(process.env.WS_PING_INTERVAL || '30000', 10),
    pingTimeout: parseInt(process.env.WS_PING_TIMEOUT || '5000', 10),
  },
};

// Validate required configuration
function validateConfig(): void {
  const required = [
    'database.url',
    'smartRouter.url',
    'gbrain.bin',
    'auth.jwtSecret',
  ];

  for (const key of required) {
    const [section, field] = key.split('.');
    const value = (config as any)[section]?.[field];
    if (!value) {
      throw new Error(`Missing required configuration: ${key}`);
    }
  }
}

validateConfig();
