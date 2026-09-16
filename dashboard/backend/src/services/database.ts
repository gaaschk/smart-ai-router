/**
 * Database service — manages PostgreSQL connections and migrations.
 */
import { Pool, Client } from 'pg';
import fs from 'fs';
import path from 'path';
import { config } from '../config';
import { log } from '../middleware/logging';

let pool: Pool | null = null;

/** Initialize database connection pool */
export function initializePool(): Pool {
  if (pool) return pool;

  pool = new Pool({
    connectionString: config.database.url,
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
  });

  pool.on('error', (err: Error) => {
    log('error', 'Unexpected error on idle client', { error: err.message });
  });

  return pool;
}

/** Get a client from the pool */
export function getPool(): Pool {
  if (!pool) {
    throw new Error('Database pool not initialized. Call initializePool first.');
  }
  return pool;
}

/** Run a query */
export async function query(sql: string, values: unknown[] = []): Promise<any> {
  const p = getPool();
  try {
    const result = await p.query(sql, values);
    return result;
  } catch (err) {
    log('error', 'Database query error', { sql, error: String(err) });
    throw err;
  }
}

/** Run migrations */
export async function runMigrations(): Promise<void> {
  const client = new Client({ connectionString: config.database.url });
  try {
    await client.connect();
    log('info', 'Connected to database for migrations');

    // Create migrations table if it doesn't exist
    await client.query(`
      CREATE TABLE IF NOT EXISTS migrations (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) UNIQUE NOT NULL,
        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    `);

    // Read and execute migrations
    const migrationsDir = path.join(__dirname, '../../migrations');
    if (!fs.existsSync(migrationsDir)) {
      log('warn', 'Migrations directory not found', { path: migrationsDir });
      return;
    }

    const files = fs.readdirSync(migrationsDir)
      .filter((f) => f.endsWith('.sql'))
      .sort();

    for (const file of files) {
      const migration = await client.query(
        'SELECT name FROM migrations WHERE name = $1',
        [file]
      );

      if (migration.rows.length > 0) {
        log('info', `Migration already executed: ${file}`);
        continue;
      }

      const filePath = path.join(migrationsDir, file);
      const sql = fs.readFileSync(filePath, 'utf-8');

      try {
        await client.query(sql);
        await client.query(
          'INSERT INTO migrations (name) VALUES ($1)',
          [file]
        );
        log('info', `Migration executed: ${file}`);
      } catch (err) {
        log('error', `Migration failed: ${file}`, { error: String(err) });
        throw err;
      }
    }

    log('info', 'All migrations completed successfully');
  } finally {
    await client.end();
  }
}

/** Close the connection pool */
export async function closePool(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
    log('info', 'Database pool closed');
  }
}
