/**
 * User service — manage users, authentication, sessions, API keys.
 */
import { hash, compare } from 'bcryptjs';
import { sign, verify } from 'jsonwebtoken';
import { randomBytes } from 'crypto';
import { v4 as uuidv4 } from 'uuid';
import { query } from './database';
import { config } from '../config';
import { User, ApiKey } from '../types';
import { AppError } from '../types';

const BCRYPT_ROUNDS = 10;
const JWT_EXPIRY = '7d';

/**
 * Create a new user.
 * @throws AppError if email is already in use.
 */
export async function createUser(
  email: string,
  name: string,
  password: string,
  role: 'admin' | 'user' = 'user'
): Promise<User> {
  // Check if user exists
  const existingRes = await query('SELECT id FROM users WHERE email = $1', [email]);
  if (existingRes.rows.length > 0) {
    throw new AppError(409, `User with email ${email} already exists`, 'USER_EXISTS');
  }

  const passwordHash = await hash(password, BCRYPT_ROUNDS);
  const id = uuidv4();

  const res = await query(
    `INSERT INTO users (id, email, name, password_hash, role)
     VALUES ($1, $2, $3, $4, $5)
     RETURNING id, email, name, role, created_at`,
    [id, email, name, passwordHash, role]
  );

  const row = res.rows[0];
  return {
    id: row.id,
    email: row.email,
    name: row.name,
    role: row.role,
    createdAt: row.created_at,
  };
}

/**
 * Authenticate user by email + password.
 * @returns user + JWT token if successful.
 * @throws AppError if credentials are invalid.
 */
export async function authenticateUser(
  email: string,
  password: string,
  ipAddress?: string
): Promise<{ user: User; token: string }> {
  const res = await query(
    'SELECT id, email, name, role, password_hash, created_at FROM users WHERE email = $1 AND active = true',
    [email]
  );

  if (res.rows.length === 0) {
    throw new AppError(401, 'Invalid email or password', 'AUTH_FAILED');
  }

  const row = res.rows[0];
  const passwordMatch = await compare(password, row.password_hash);

  if (!passwordMatch) {
    throw new AppError(401, 'Invalid email or password', 'AUTH_FAILED');
  }

  // Update last_login
  await query('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = $1', [row.id]);

  const user: User = {
    id: row.id,
    email: row.email,
    name: row.name,
    role: row.role,
    createdAt: row.created_at,
  };

  // Create JWT token
  const token = sign(
    { sub: user.id, email: user.email, role: user.role },
    config.auth.jwtSecret,
    { expiresIn: JWT_EXPIRY }
  );

  // Record session
  const tokenHash = await hash(token, 1); // Quick hash for storage
  const expiresAt = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000); // 7 days
  await query(
    `INSERT INTO sessions (user_id, token_hash, expires_at, ip_address)
     VALUES ($1, $2, $3, $4)`,
    [user.id, tokenHash, expiresAt, ipAddress || null]
  );

  return { user, token };
}

/**
 * Verify and decode a JWT token.
 * @throws AppError if token is invalid or expired.
 */
export function verifyToken(token: string): { sub: string; email: string; role: string } {
  try {
    const decoded = verify(token, config.auth.jwtSecret) as any;
    return {
      sub: decoded.sub,
      email: decoded.email,
      role: decoded.role,
    };
  } catch (err: any) {
    throw new AppError(401, 'Invalid or expired token', 'INVALID_TOKEN');
  }
}

/**
 * Get user by ID.
 */
export async function getUserById(userId: string): Promise<User | null> {
  const res = await query(
    `SELECT id, email, name, role, created_at, last_login
     FROM users WHERE id = $1 AND active = true`,
    [userId]
  );

  if (res.rows.length === 0) return null;

  const row = res.rows[0];
  return {
    id: row.id,
    email: row.email,
    name: row.name,
    role: row.role,
    createdAt: row.created_at,
    lastLogin: row.last_login,
  };
}

/**
 * List all users (admin only).
 */
export async function listUsers(limit = 50, offset = 0): Promise<{ users: User[]; total: number }> {
  const res = await query(
    `SELECT id, email, name, role, created_at, last_login
     FROM users WHERE active = true
     ORDER BY created_at DESC LIMIT $1 OFFSET $2`,
    [limit, offset]
  );

  const countRes = await query('SELECT COUNT(*) as count FROM users WHERE active = true');

  return {
    users: res.rows.map((row: any) => ({
      id: row.id,
      email: row.email,
      name: row.name,
      role: row.role,
      createdAt: row.created_at,
      lastLogin: row.last_login,
    })),
    total: parseInt(countRes.rows[0].count, 10),
  };
}

/**
 * Create an API key for a user.
 */
export async function createApiKey(userId: string, name: string): Promise<{ key: string; id: string }> {
  const rawKey = randomBytes(32).toString('hex');
  const keyHash = await hash(rawKey, BCRYPT_ROUNDS);
  const id = uuidv4();

  await query(
    `INSERT INTO api_keys (id, user_id, name, key_hash)
     VALUES ($1, $2, $3, $4)`,
    [id, userId, name, keyHash]
  );

  return { id, key: rawKey }; // Return raw key only once; don't store it
}

/**
 * Verify an API key (for header-based auth).
 */
export async function verifyApiKey(rawKey: string): Promise<string | null> {
  const res = await query(
    `SELECT id, user_id, key_hash FROM api_keys WHERE active = true AND revoked_at IS NULL`
  );

  for (const row of res.rows) {
    const match = await compare(rawKey, row.key_hash);
    if (match) {
      // Update last_used
      await query('UPDATE api_keys SET last_used = CURRENT_TIMESTAMP WHERE id = $1', [row.id]);
      return row.user_id;
    }
  }

  return null;
}

/**
 * List API keys for a user.
 */
export async function listApiKeys(userId: string): Promise<Omit<ApiKey, 'keyHash'>[]> {
  const res = await query(
    `SELECT id, user_id, name, created_at, last_used, revoked_at
     FROM api_keys WHERE user_id = $1 AND active = true
     ORDER BY created_at DESC`,
    [userId]
  );

  return res.rows.map((row: any) => ({
    id: row.id,
    userId: row.user_id,
    name: row.name,
    createdAt: row.created_at,
    lastUsed: row.last_used,
    revokedAt: row.revoked_at,
  }));
}

/**
 * Revoke an API key.
 */
export async function revokeApiKey(keyId: string, userId: string): Promise<void> {
  const res = await query(
    `UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP
     WHERE id = $1 AND user_id = $2
     RETURNING id`,
    [keyId, userId]
  );

  if (res.rows.length === 0) {
    throw new AppError(404, 'API key not found', 'API_KEY_NOT_FOUND');
  }
}
