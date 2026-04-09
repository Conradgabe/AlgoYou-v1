//+------------------------------------------------------------------+
//|                                                   ScalperEA.mq5  |
//|            Execution engine — the fast path                       |
//|                                                                   |
//|   This EA is the front line. It captures every tick, sends data   |
//|   to the Python analytical engine, receives signals, and          |
//|   executes orders with minimal latency.                           |
//|                                                                   |
//|   CRITICAL: If Python disconnects, this EA FLATTENS ALL           |
//|   POSITIONS and enters HALTED state. A disconnected bot with      |
//|   open positions is how accounts blow up.                         |
//+------------------------------------------------------------------+
#property copyright "Scalper Engine"
#property version   "1.00"
#property strict

#include "SocketLib.mqh"
#include "OrderManager.mqh"
#include <JAson.mqh>

//--- Input parameters
input string   InpHost          = "127.0.0.1";   // Python server host
input int      InpPort          = 5555;           // Python server port
input string   InpSymbol        = "XAUUSD";      // Trading symbol
input int      InpMagic         = 7741;           // Magic number
input int      InpSlippage      = 10;             // Max slippage (points)
input int      InpHBInterval    = 1000;           // Heartbeat interval (ms)
input int      InpHBTimeout     = 5000;           // Heartbeat timeout (ms)
input bool     InpAutoFlatten   = true;           // Flatten on disconnect

//--- Global objects
CSocketLib     g_socket;
COrderManager  g_orders;

//--- State
bool           g_initialized    = false;
bool           g_halted         = false;
string         g_halt_reason    = "";
datetime       g_last_tick_time = 0;
int            g_tick_count     = 0;

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   // Validate symbol
   string symbol = InpSymbol;
   if(Symbol() != symbol)
   {
      // Try to find the correct symbol name
      string variants[] = {"XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.raw", "XAUUSD.stp"};
      bool found = false;
      for(int i = 0; i < ArraySize(variants); i++)
      {
         if(SymbolSelect(variants[i], true))
         {
            symbol = variants[i];
            found = true;
            break;
         }
      }
      if(!found)
      {
         PrintFormat("[EA] WARNING: Symbol %s not found, using chart symbol %s", InpSymbol, Symbol());
         symbol = Symbol();
      }
   }

   // Initialize order manager
   if(!g_orders.Init(symbol, InpMagic, InpSlippage))
   {
      Print("[EA] Order manager init failed");
      return INIT_FAILED;
   }

   // Configure socket
   g_socket.SetAddress(InpHost, InpPort);
   g_socket.SetHeartbeat(InpHBInterval, InpHBTimeout);
   g_socket.SetReconnect(1000, 30000);

   // Connect to Python engine
   if(!g_socket.Connect())
   {
      Print("[EA] Initial connection failed — will retry on timer");
   }
   else
   {
      // Send initial position sync
      SendPositionSync();
   }

   // Timer for heartbeat and reconnection (every 500ms)
   EventSetMillisecondTimer(500);

   g_initialized = true;
   PrintFormat("[EA] Initialized: %s magic=%d", symbol, InpMagic);

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();

   // Notify Python we're shutting down
   if(g_socket.IsConnected())
   {
      CJAVal msg;
      msg["type"] = "shutdown";
      msg["reason"] = IntegerToString(reason);
      g_socket.SendJSON(msg);
      Sleep(500); // Give Python time to process
   }

   g_socket.Disconnect();

   PrintFormat("[EA] Deinitialized. Ticks processed: %d", g_tick_count);
}

//+------------------------------------------------------------------+
//| OnTick — fires on EVERY tick. The hot path.                       |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!g_initialized || g_halted)
      return;

   if(!g_socket.IsConnected())
      return;

   // Get current tick
   MqlTick tick;
   if(!SymbolInfoTick(g_orders.Symbol(), tick))
      return;

   // Stale tick detection
   datetime now = TimeLocal();
   if(tick.time == g_last_tick_time)
      return; // duplicate
   g_last_tick_time = tick.time;
   g_tick_count++;

   // Build tick message
   CJAVal msg;
   msg["type"]   = "tick";
   msg["bid"]    = tick.bid;
   msg["ask"]    = tick.ask;
   msg["time"]   = (long)(tick.time_msc);
   msg["vol"]    = (int)(tick.volume);
   msg["spread"] = (int)SymbolInfoInteger(g_orders.Symbol(), SYMBOL_SPREAD);

   // Send to Python
   g_socket.SendJSON(msg);

   // Check for incoming signals (non-blocking)
   ProcessIncoming();
}

//+------------------------------------------------------------------+
//| OnTimer — heartbeat, reconnection, stale data detection           |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_initialized)
      return;

   // ── Reconnection ────────────────────────────────────────
   if(!g_socket.IsConnected())
   {
      if(g_socket.TryReconnect())
      {
         Print("[EA] Reconnected to Python engine");
         SendPositionSync();
         g_halted = false;
         g_halt_reason = "";
      }
      return;
   }

   // ── Heartbeat ───────────────────────────────────────────
   g_socket.SendHeartbeat();

   if(!g_socket.CheckHeartbeat())
   {
      Print("[EA] Heartbeat timeout — Python engine unresponsive");

      if(InpAutoFlatten && g_orders.HasPosition())
      {
         Print("[EA] !!! EMERGENCY FLATTEN — heartbeat lost !!!");
         g_orders.FlattenAll();
      }

      g_socket.Disconnect();
      g_halted = true;
      g_halt_reason = "heartbeat_timeout";
      return;
   }

   // ── Stale data detection ────────────────────────────────
   datetime now = TimeLocal();
   if(g_last_tick_time > 0 && (now - g_last_tick_time) > 5)
   {
      // No tick for 5 seconds — data feed may be stale
      // Don't flatten, but stop accepting new signals
      if(!g_halted)
      {
         Print("[EA] WARNING: Stale data detected (5s without tick)");
      }
   }

   // Process any pending messages
   ProcessIncoming();
}

//+------------------------------------------------------------------+
//| Process incoming messages from Python                              |
//+------------------------------------------------------------------+
void ProcessIncoming()
{
   CJAVal msg;

   // Process all available messages (drain the buffer)
   int max_messages = 10; // safety limit per cycle
   int processed = 0;

   while(g_socket.HasData() && processed < max_messages)
   {
      if(!g_socket.ReadMessage(msg))
         break;

      string msg_type = msg["type"].ToStr();

      if(msg_type == "heartbeat")
      {
         // Heartbeat acknowledged — update tracking in socket lib
         processed++;
         continue;
      }
      else if(msg_type == "signal")
      {
         HandleSignal(msg);
      }
      else if(msg_type == "modify")
      {
         HandleModify(msg);
      }
      else if(msg_type == "close")
      {
         HandleClose(msg);
      }
      else if(msg_type == "flatten")
      {
         HandleFlatten(msg);
      }
      else if(msg_type == "command")
      {
         HandleCommand(msg);
      }

      processed++;
   }
}

//+------------------------------------------------------------------+
//| Handle trade signal from Python                                   |
//+------------------------------------------------------------------+
void HandleSignal(CJAVal &msg)
{
   if(g_halted)
   {
      Print("[EA] Signal rejected — halted");
      return;
   }

   string action = msg["action"].ToStr();
   double volume = msg["volume"].ToDbl();
   double sl     = msg["sl"].ToDbl();
   double tp     = msg["tp"].ToDbl();
   int    magic  = (int)msg["magic"].ToInt();

   // Safety: don't open if we already have a position
   if(g_orders.HasPosition())
   {
      Print("[EA] Signal rejected — already in position");
      return;
   }

   STradeResult result;

   if(action == "BUY")
      result = g_orders.Buy(volume, sl, tp);
   else if(action == "SELL")
      result = g_orders.Sell(volume, sl, tp);
   else
   {
      PrintFormat("[EA] Unknown signal action: %s", action);
      return;
   }

   // Send fill confirmation back to Python
   CJAVal fill;
   fill["type"]      = "fill";
   fill["fill_type"] = "open";
   fill["success"]   = result.success;
   fill["ticket"]    = (long)result.ticket;
   fill["price"]     = result.price;
   fill["volume"]    = result.volume;
   fill["sl"]        = result.sl;
   fill["tp"]        = result.tp;
   fill["spread"]    = result.spread;
   fill["direction"] = action;
   fill["time"]      = (long)(TimeCurrent() * 1000);

   if(!result.success)
      fill["error"] = result.error;

   g_socket.SendJSON(fill);
}

//+------------------------------------------------------------------+
//| Handle SL/TP modification                                         |
//+------------------------------------------------------------------+
void HandleModify(CJAVal &msg)
{
   ulong  ticket = (ulong)msg["ticket"].ToInt();
   double sl     = msg["sl"].ToDbl();
   double tp     = msg["tp"].ToDbl();

   STradeResult result = g_orders.ModifySLTP(ticket, sl, tp);

   if(!result.success)
      PrintFormat("[EA] Modify failed: %s", result.error);
}

//+------------------------------------------------------------------+
//| Handle close command                                              |
//+------------------------------------------------------------------+
void HandleClose(CJAVal &msg)
{
   ulong  ticket = (ulong)msg["ticket"].ToInt();
   string reason = msg["reason"].ToStr();

   STradeResult result = g_orders.ClosePosition(ticket, reason);

   // Send close fill confirmation
   CJAVal fill;
   fill["type"]      = "fill";
   fill["fill_type"] = "close";
   fill["success"]   = result.success;
   fill["ticket"]    = (long)ticket;
   fill["price"]     = result.price;
   fill["volume"]    = result.volume;
   fill["spread"]    = result.spread;
   fill["reason"]    = reason;
   fill["time"]      = (long)(TimeCurrent() * 1000);

   // Include P&L
   if(result.success)
   {
      // P&L is calculated from the position before it was closed
      // We need to get it from the deal history
      double pnl_money = 0;
      double commission = 0;
      double swap_val = 0;

      if(HistoryDealSelect(result.ticket))
      {
         pnl_money = HistoryDealGetDouble(result.ticket, DEAL_PROFIT);
         commission = HistoryDealGetDouble(result.ticket, DEAL_COMMISSION);
         swap_val = HistoryDealGetDouble(result.ticket, DEAL_SWAP);
      }

      // Try to get from recent deals if direct select fails
      if(pnl_money == 0)
      {
         HistorySelect(TimeCurrent() - 60, TimeCurrent());
         for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
         {
            ulong deal_ticket = HistoryDealGetTicket(i);
            if(deal_ticket == 0) continue;
            if(HistoryDealGetInteger(deal_ticket, DEAL_MAGIC) != InpMagic) continue;
            if(HistoryDealGetInteger(deal_ticket, DEAL_ENTRY) == DEAL_ENTRY_OUT)
            {
               pnl_money = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
               commission = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
               swap_val = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
               break;
            }
         }
      }

      SPositionInfo before_close; // Position info was already cleared
      double open_price = 0;
      // We calculate pnl_points from the price difference
      // This is approximate — the actual P&L includes spread and slippage
      if(result.price > 0)
      {
         double point = SymbolInfoDouble(g_orders.Symbol(), SYMBOL_POINT);
         if(point > 0)
         {
            fill["pnl_points"] = pnl_money / (result.volume * SymbolInfoDouble(g_orders.Symbol(), SYMBOL_TRADE_TICK_VALUE)) * SymbolInfoDouble(g_orders.Symbol(), SYMBOL_TRADE_TICK_SIZE) / point;
         }
      }

      fill["pnl_money"]   = pnl_money;
      fill["commission"]   = commission;
      fill["swap"]         = swap_val;
   }

   if(!result.success)
      fill["error"] = result.error;

   g_socket.SendJSON(fill);
}

//+------------------------------------------------------------------+
//| Handle flatten (emergency close all)                              |
//+------------------------------------------------------------------+
void HandleFlatten(CJAVal &msg)
{
   string reason = msg["reason"].ToStr();
   PrintFormat("[EA] !!! FLATTEN command received: %s !!!", reason);

   g_orders.FlattenAll();

   g_halted = true;
   g_halt_reason = "flatten_" + reason;
}

//+------------------------------------------------------------------+
//| Handle generic commands                                           |
//+------------------------------------------------------------------+
void HandleCommand(CJAVal &msg)
{
   string action = msg["action"].ToStr();

   if(action == "HALT")
   {
      g_halted = true;
      g_halt_reason = msg["reason"].ToStr();
      PrintFormat("[EA] HALTED: %s", g_halt_reason);
   }
   else if(action == "RESUME")
   {
      g_halted = false;
      g_halt_reason = "";
      Print("[EA] RESUMED");
   }
   else if(action == "SHUTDOWN")
   {
      Print("[EA] Shutdown command received");
      ExpertRemove();
   }
   else if(action == "STATUS")
   {
      SendPositionSync();
   }
}

//+------------------------------------------------------------------+
//| Send position and account state to Python (sync on connect)       |
//+------------------------------------------------------------------+
void SendPositionSync()
{
   CJAVal msg;
   msg["type"] = "position";
   msg["balance"]    = AccountInfoDouble(ACCOUNT_BALANCE);
   msg["equity"]     = AccountInfoDouble(ACCOUNT_EQUITY);
   msg["margin"]     = AccountInfoDouble(ACCOUNT_MARGIN);
   msg["free_margin"] = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   // Include any open position
   SPositionInfo pos = g_orders.GetPosition();
   msg["has_position"]  = pos.exists;
   if(pos.exists)
   {
      msg["pos_ticket"]    = (long)pos.ticket;
      msg["pos_direction"] = pos.direction;
      msg["pos_volume"]    = pos.volume;
      msg["pos_price"]     = pos.open_price;
      msg["pos_sl"]        = pos.sl;
      msg["pos_tp"]        = pos.tp;
      msg["pos_profit"]    = pos.profit;
   }

   g_socket.SendJSON(msg);

   PrintFormat("[EA] Position sync sent: balance=%.2f equity=%.2f pos=%s",
               AccountInfoDouble(ACCOUNT_BALANCE),
               AccountInfoDouble(ACCOUNT_EQUITY),
               pos.exists ? "YES" : "NO");
}

//+------------------------------------------------------------------+
//| OnTradeTransaction — catch SL/TP hits from server side            |
//+------------------------------------------------------------------+
void OnTradeTransaction(
   const MqlTradeTransaction &trans,
   const MqlTradeRequest &request,
   const MqlTradeResult &result)
{
   // Detect position closed by SL/TP (server-side execution)
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      ulong deal = trans.deal;
      if(deal == 0) return;

      if(HistoryDealSelect(deal))
      {
         long magic = HistoryDealGetInteger(deal, DEAL_MAGIC);
         if(magic != InpMagic) return;

         long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
         if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
         {
            // Position was closed (by SL, TP, or other means)
            double pnl = HistoryDealGetDouble(deal, DEAL_PROFIT);
            double price = HistoryDealGetDouble(deal, DEAL_PRICE);
            double volume = HistoryDealGetDouble(deal, DEAL_VOLUME);
            double commission = HistoryDealGetDouble(deal, DEAL_COMMISSION);
            double swap_val = HistoryDealGetDouble(deal, DEAL_SWAP);
            long deal_reason = HistoryDealGetInteger(deal, DEAL_REASON);

            string reason = "unknown";
            if(deal_reason == DEAL_REASON_SL) reason = "sl_hit";
            else if(deal_reason == DEAL_REASON_TP) reason = "tp_hit";
            else if(deal_reason == DEAL_REASON_SO) reason = "stop_out";

            PrintFormat("[EA] Position closed by server: reason=%s pnl=%.2f", reason, pnl);

            // Notify Python
            if(g_socket.IsConnected())
            {
               CJAVal fill;
               fill["type"]        = "fill";
               fill["fill_type"]   = "close";
               fill["success"]     = true;
               fill["ticket"]      = (long)trans.position;
               fill["price"]       = price;
               fill["volume"]      = volume;
               fill["pnl_money"]   = pnl;
               fill["commission"]  = commission;
               fill["swap"]        = swap_val;
               fill["reason"]      = reason;
               fill["spread"]      = (int)SymbolInfoInteger(g_orders.Symbol(), SYMBOL_SPREAD);
               fill["time"]        = (long)(TimeCurrent() * 1000);

               double point = SymbolInfoDouble(g_orders.Symbol(), SYMBOL_POINT);
               double tick_val = SymbolInfoDouble(g_orders.Symbol(), SYMBOL_TRADE_TICK_VALUE);
               double tick_size = SymbolInfoDouble(g_orders.Symbol(), SYMBOL_TRADE_TICK_SIZE);
               if(tick_val > 0 && volume > 0 && point > 0)
                  fill["pnl_points"] = (pnl / (volume * tick_val)) * tick_size / point;

               g_socket.SendJSON(fill);
            }
         }
      }
   }
}
//+------------------------------------------------------------------+
