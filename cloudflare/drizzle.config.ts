import {defineConfig} from "drizzle-kit";
export default defineConfig({out:"./cloudflare/migrations",schema:"./cloudflare/schema.ts",dialect:"sqlite"});
