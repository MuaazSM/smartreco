/**
 * TypeScript mirrors of the backend response/request shapes (Pydantic models in
 * `app/api/routes/*.py`). Kept hand-written and minimal rather than generated — the backend is owned
 * by other phases running concurrently, so this file only encodes the *contract*, not the
 * implementation. Field names/types must stay in lockstep with the Python schemas they mirror.
 */

// ---- auth (app/api/routes/auth.py) ----------------------------------------------------------------

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  role: "user" | "admin" | string;
  digest_optin: boolean;
  created_at: string;
}

// ---- catalog (app/api/routes/products.py) ----------------------------------------------------------

export interface ProductOut {
  id: string;
  title: string;
  description: string;
  category: string;
  tags: string[];
  level: "beginner" | "intermediate" | "advanced" | string;
  price_cents: number;
  is_active: boolean;
  vector_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProductPage {
  items: ProductOut[];
  total: number;
  page: number;
  page_size: number;
}

// ---- recommendations (app/api/routes/recommendations.py) -------------------------------------------

export interface RecommendationItem {
  product_id: string;
  reason: string;
  title: string;
  category: string;
  level: string;
  price_cents: number;
}

export interface Transparency {
  total_events: number;
  searches: number;
  events_since_gen: number;
  last_generated_at: string | null;
  top_categories: Record<string, number>;
}

export interface CurrentRecommendationOut {
  recommendation_id: string;
  headline: string;
  narrative: string;
  items: RecommendationItem[];
  trigger_reason: string;
  created_at: string;
  transparency: Transparency;
}

export interface RefreshOut {
  recommendation_id: string | null;
  cache_hit: string | null;
  status: string;
}

export type FeedbackSignal = "up" | "down";

// ---- admin (app/api/routes/admin.py) ----------------------------------------------------------------

export interface ProductCreateIn {
  title: string;
  description?: string;
  category: string;
  tags?: string[];
  level: "beginner" | "intermediate" | "advanced";
  price_cents?: number;
  is_active?: boolean;
}

export type ProductUpdateIn = Partial<ProductCreateIn>;

export interface SyncStatus {
  in_sync: boolean;
  missing_in_vector: string[];
  orphaned_in_vector: string[];
  outbox_lag_seconds: number;
  pending_count: number;
  failed_count: number;
}
