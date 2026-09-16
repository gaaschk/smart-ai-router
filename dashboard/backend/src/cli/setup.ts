#!/usr/bin/env node
/**
 * Dashboard Setup CLI
 *
 * First-run initialization: creates PostgreSQL tables, admin user, and validates setup.
 * Run this once before starting the server.
 *
 * Usage:
 *   npx ts-node src/cli/setup.ts
 *   npm run setup
 */

import * as readline from 'readline';
import { config } from '../config';
import { initializePool, runMigrations, closePool } from '../services/database';
import { createUser } from '../services/userService';

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
});

function prompt(question: string): Promise<string> {
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      resolve(answer.trim());
    });
  });
}

async function setup(): Promise<void> {
  console.log('\n📋 Dashboard Setup\n');
  console.log('This script will initialize your dashboard for first use.\n');

  // 1. Verify environment
  console.log('1️⃣  Verifying environment...');
  const requiredEnvVars = ['DATABASE_URL', 'AUTH_JWT_SECRET'];
  const missing = requiredEnvVars.filter((key) => !process.env[key]);

  if (missing.length > 0) {
    console.error(`❌ Missing environment variables: ${missing.join(', ')}`);
    console.error(`\n   Create a .env file with at least:\n`);
    console.error(`   DATABASE_URL=postgresql://user:pass@localhost:5432/dashboard`);
    console.error(`   AUTH_JWT_SECRET=your-secret-key\n`);
    process.exit(1);
  }
  console.log('✅ Environment verified\n');

  // 2. Initialize database
  console.log('2️⃣  Initializing database...');
  try {
    initializePool();
    await runMigrations();
    console.log('✅ Database initialized & migrations run\n');
  } catch (err: any) {
    console.error(`❌ Database setup failed: ${err.message}`);
    console.error(
      '\n   Make sure PostgreSQL is running and DATABASE_URL is correct:\n'
    );
    console.error(`   Current: ${config.database.url}\n`);
    process.exit(1);
  }

  // 3. Check for existing admin
  console.log('3️⃣  Checking for admin user...');
  const { query } = await import('../services/database');
  try {
    const admins = await query("SELECT COUNT(*) as count FROM users WHERE role = 'admin'");
    const adminCount = parseInt(admins.rows[0].count || 0, 10);

    if (adminCount > 0) {
      console.log(`✅ Admin user already exists (${adminCount} admin(s) found)\n`);
    } else {
      // 4. Create admin user
      console.log('4️⃣  No admin user found. Creating one...\n');

      const email = await prompt('   Admin email: ');
      if (!email.includes('@')) {
        console.error('\n❌ Invalid email format');
        process.exit(1);
      }

      const name = await prompt('   Admin name: ');
      const password = await prompt('   Admin password (8+ chars): ');

      if (password.length < 8) {
        console.error('\n❌ Password must be at least 8 characters');
        process.exit(1);
      }

      try {
        const admin = await createUser(email, name, password, 'admin');
        console.log(`\n✅ Admin user created: ${admin.email} (${admin.name})\n`);
      } catch (err: any) {
        console.error(`\n❌ Failed to create admin: ${err.message}\n`);
        process.exit(1);
      }
    }
  } catch (err: any) {
    console.error(`❌ Failed to check admin status: ${err.message}`);
    process.exit(1);
  }

  // 5. Summary
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log('✅ Setup Complete!\n');
  console.log('Next steps:');
  console.log('  1. Ensure Smart-AI-Router is running on port 8001');
  console.log('  2. Ensure GBrain is installed & configured');
  console.log('  3. Start the dashboard backend:');
  console.log('     npm run dev');
  console.log('  4. Start the dashboard frontend (in another terminal):');
  console.log('     cd dashboard/frontend && npm run dev');
  console.log('  5. Open http://localhost:5173 and sign in with your admin credentials\n');
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n');

  await closePool();
  rl.close();
}

setup().catch((err) => {
  console.error(`\n❌ Setup failed: ${err.message}\n`);
  process.exit(1);
});
