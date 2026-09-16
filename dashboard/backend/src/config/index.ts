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
  //
  // The dashboard is accessed from whatever host it's deployed on (localhost
  // in dev, `<hostname>.local`, or a LAN IP on the Mac Mini) plus, optionally,
  // an explicit CORS_ORIGIN override. Rather than hardcode one host, allow any
  // origin whose hostname is localhost/127.0.0.1, ends in `.local` (mDNS), or
  // is a private LAN IP (192.168.x.x / 10.x.x.x / 172.16-31.x.x) -- all normal
  // ways to reach a home-network Mac Mini -- plus CORS_ORIGIN if set.
  cors: {
    allowedOrigin(origin: string): boolean {
      if (process.env.CORS_ORIGIN && origin === process.env.CORS_ORIGIN) return true;
      try {
        const { hostname } = new URL(origin);
        if (hostname === 'localhost' || hostname === '127.0.0.1') return true;
        if (hostname.endsWith('.local')) return true;
        if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(hostname)) return true;
        if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(hostname)) return true;
        if (/^172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(hostname)) return true;
      } catch {
        // Not a parseable URL (e.g. non-browser client with no Origin header)
      }
      return false;
    },
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
