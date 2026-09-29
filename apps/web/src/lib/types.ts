/** Shared API types, mirroring apps/api/app/schemas.py. */

export type TradingMode = 'DEMO' | 'PAPER' | 'LIVE';
export type BotState =
  | 'OFFLINE' | 'STARTING' | 'RUNNING' | 'PAUSING' | 'PAUSED'
  | 'STOPPING' | 'STOPPED' | 'ERROR' | 'EMERGENCY_STOP';

export interface User {
  id: string;
  email: string;
  name: string;
  country: string;
  role: string;
  is_active: boolean;
  is_email_verified: boolean;
  two_factor_enabled: boolean;
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResponse {
  user: User;
  tokens: TokenPair;
}

export interface Bot {
  id: string;
  name: string;
  symbol: string;
  strategy: string;
  timeframe: string;
  mode: TradingMode;
  state: BotState;
  auto_start: boolean;
  live_granted: boolean;
  broker_connection_id: string | null;
  risk_config: Record<string, unknown>;
  trailing_config: Record<string, unknown>;
  stats: Record<string, unknown>;
  last_heartbeat_at: string | null;
  created_at: string;
}

export interface Position {
  id: string;
  bot_id: string | null;
  symbol: string;
  side: 'BUY' | 'SELL';
  volume: string;
  open_price: string;
  current_price: string;
  stop_loss: string | null;
  take_profit: string | null;
  unrealized_pnl: string;
  realized_pnl: string;
  commission: string;
  swap: string;
  status: string;
  mode: TradingMode;
  break_even_done: boolean;
  opened_at: string;
  closed_at: string | null;
}

export interface Trade {
  id: string;
  position_id: string | null;
  bot_id: string | null;
  symbol: string;
  side: 'BUY' | 'SELL';
  volume: string;
  open_price: string;
  close_price: string;
  commission: string;
  swap: string;
  pnl: string;
  mode: TradingMode;
  result: string;
  r_multiple: string | null;
  close_reason: string | null;
  opened_at: string;
  closed_at: string;
}

export interface Order {
  id: string;
  client_order_id: string;
  broker_order_id: string | null;
  symbol: string;
  side: 'BUY' | 'SELL';
  order_type: string;
  volume: string;
  filled_volume: string;
  status: string;
  requested_price: string | null;
  filled_price: string | null;
  stop_loss: string | null;
  take_profit: string | null;
  commission: string;
  reject_reason: string | null;
  mode: TradingMode;
  created_at: string;
}

export interface PerformanceSummary {
  balance: number;
  equity: number;
  open_positions: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  net_pnl: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor: number | null;
  realized_today: number;
  active_bots: number;
  total_bots: number;
}

export interface EquityPoint {
  t: string;
  equity: number;
  realized: number;
}

export interface SymbolInfo {
  symbol: string;
  digits: number;
  pip_size: number;
  min_volume: number;
  max_volume: number;
  volume_step: number;
  max_spread_pips: number;
  /** Market family: forex, metal or crypto. */
  asset_class: string;
}

export interface Quote {
  symbol: string;
  bid: number;
  ask: number;
  mid: number;
  spread_pips: number;
  timestamp: string;
}

export interface Health {
  status: 'ok' | 'degraded';
  version: string;
  environment: string;
  database: boolean;
  redis: boolean;
  trading_mode: TradingMode;
  live_trading_enabled: boolean;
  time: string;
}

export interface BrokerConnection {
  id: string;
  name: string;
  broker: string;
  is_demo: boolean;
  masked_login: string;
  connection_state: string;
  last_error: string | null;
  last_synced_at: string | null;
  balance: number | null;
  equity: number | null;
  currency: string;
  created_at: string;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
  balance: number | null;
  equity: number | null;
  currency: string | null;
  latency_ms: number | null;
}

export interface BrokerConnectionInput {
  name: string;
  broker: string;
  is_demo: boolean;
  login: string;
  password: string;
  server: string;
  currency: string;
}

// --- Performance analytics -------------------------------------------------
// Optional metrics are null when the data cannot support them (e.g. profit
// factor with no losing trades), so the UI must render "not available"
// rather than substituting a flattering number.
export interface PerformanceMetrics {
  period_days: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  win_rate: number;
  net_pnl: number;
  gross_profit: number;
  gross_loss: number;
  avg_win: number;
  avg_loss: number;
  largest_win: number;
  largest_loss: number;
  expectancy: number;
  payoff_ratio: number | null;
  profit_factor: number | null;
  longest_win_streak: number;
  longest_loss_streak: number;
  max_drawdown: number;
  max_drawdown_percent: number;
  sharpe_pnl_ratio: number | null;
  trading_days: number;
  profitable_days: number;
  best_day: number;
  worst_day: number;
}

export interface EquityPointDaily {
  date: string | null;
  daily_pnl: number;
  cumulative_pnl: number;
}

export interface SymbolPerformance {
  symbol: string;
  trades: number;
  win_rate: number;
  net_pnl: number;
  expectancy: number;
  profit_factor: number | null;
}

export interface StrategyPerformance {
  strategy: string;
  trades: number;
  win_rate: number;
  net_pnl: number;
  expectancy: number;
  profit_factor: number | null;
}

export interface AnalyticsReport {
  metrics: PerformanceMetrics;
  equity: EquityPointDaily[];
  by_symbol: SymbolPerformance[];
  by_strategy: StrategyPerformance[];
}

// --- Administration --------------------------------------------------------
export interface PlatformStats {
  users: number;
  active_users: number;
  admins: number;
  bots: number;
  running_bots: number;
  broker_connections: number;
  connected_brokers: number;
  open_positions: number;
  trades: number;
}

export interface AdminUser {
  id: string;
  email: string;
  name: string;
  country: string;
  role: string;
  is_active: boolean;
  is_email_verified: boolean;
  last_login_at: string | null;
  created_at: string | null;
  bots: number;
  broker_connections: number;
}

export interface AdminUserList {
  total: number;
  items: AdminUser[];
}

export interface AdminBroker {
  id: string;
  user_id: string;
  user_email: string;
  name: string;
  broker: string;
  is_demo: boolean;
  connection_state: string;
  balance: number | null;
  equity: number | null;
  last_synced_at: string | null;
  last_error: string | null;
}

export interface AdminBot {
  id: string;
  user_id: string;
  user_email: string;
  name: string;
  symbol: string;
  strategy: string;
  timeframe: string;
  mode: string;
  state: string;
  live_granted: boolean;
  created_at: string | null;
}

/** Backtesting. Mirrors BacktestCreate / BacktestResultOut in apps/api/app/schemas.py. */

export type BacktestStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

export interface BacktestRequest {
  strategy: string;
  symbol: string;
  timeframe: string;
  date_from: string;
  date_to: string;
  initial_capital: number;
  risk_percent: number;
  spread_pips?: number;
  slippage_pips?: number;
  commission_per_lot?: number;
}

export interface BacktestJob {
  id: string;
  strategy: string;
  symbol: string;
  timeframe: string;
  date_from: string;
  date_to: string;
  initial_capital: number;
  risk_percent: number;
  spread_pips: number;
  slippage_pips: number;
  commission_per_lot: number;
  status: BacktestStatus;
  progress: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

/** A single fill from the simulated run. Keys mirror the engine's TradeRecord. */
export interface BacktestTrade {
  symbol?: string | null;
  entry_time: string | null;
  exit_time: string | null;
  side: string | null;
  volume: number | null;
  entry_price: number | null;
  exit_price: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  pnl: number | null;
  commission?: number | null;
  swap?: number | null;
  /** Why the position closed: stop, target, trailing, or end of data. */
  close_reason: string | null;
  r_multiple?: number | null;
  bars_held?: number | null;
}

/** Keys must match the engine's output in backtest/engine.py. */
export interface EquityPointBacktest {
  t: string;
  equity: number;
}

export interface DrawdownPoint {
  t: string;
  drawdown_pct: number;
}

export interface MonthlyReturn {
  month: string;
  return_pct: number;
  pnl: number;
}

export interface BacktestResult {
  final_equity: number;
  net_pnl: number;
  roi_percent: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  /** null when there were no trades. A 0 here would read as "awful", not
   *  "not measurable", so the UI must handle null explicitly. */
  profit_factor: number | null;
  max_drawdown_percent: number;
  max_drawdown_amount: number;
  average_trade: number;
  best_trade: number;
  worst_trade: number;
  avg_win: number;
  avg_loss: number;
  total_commission: number;
  equity_curve: EquityPointBacktest[];
  drawdown_curve: DrawdownPoint[];
  monthly_returns: MonthlyReturn[];
  trades: BacktestTrade[];
}

export interface BacktestDetail extends BacktestJob {
  result: BacktestResult | null;
}

/** Options come from the engine's registry, never a hardcoded client list. */
export interface BacktestOptions {
  strategies: { value: string; label: string }[];
  timeframes: string[];
  symbols: string[];
  defaults: {
    initial_capital: number;
    risk_percent: number;
    spread_pips: number;
    slippage_pips: number;
    commission_per_lot: number;
  };
}
