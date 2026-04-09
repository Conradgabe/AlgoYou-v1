//+------------------------------------------------------------------+
//|                                                 OrderManager.mqh |
//|                    Order execution, position tracking, fills      |
//+------------------------------------------------------------------+
#ifndef ORDER_MANAGER_MQH
#define ORDER_MANAGER_MQH

#include <Trade/Trade.mqh>

//--- Trade result structure
struct STradeResult
{
   bool     success;
   ulong    ticket;
   double   price;
   double   volume;
   double   sl;
   double   tp;
   int      spread;
   string   error;
};

//--- Position info structure
struct SPositionInfo
{
   bool     exists;
   ulong    ticket;
   string   direction;
   double   volume;
   double   open_price;
   double   sl;
   double   tp;
   double   profit;
   double   commission;
   double   swap;
   int      spread;
};

//+------------------------------------------------------------------+
//| Order Manager class                                               |
//+------------------------------------------------------------------+
class COrderManager
{
private:
   CTrade            m_trade;
   string            m_symbol;
   int               m_magic;
   int               m_slippage;
   ENUM_ORDER_TYPE_FILLING m_filling;

   // Active position tracking
   ulong             m_position_ticket;
   double            m_position_volume;
   string            m_position_direction;

public:
                     COrderManager();
                    ~COrderManager() {}

   // Initialization
   bool              Init(string symbol, int magic, int slippage_points = 10);

   // Order execution
   STradeResult      Buy(double volume, double sl, double tp);
   STradeResult      Sell(double volume, double sl, double tp);
   STradeResult      ClosePosition(ulong ticket, string reason = "");
   STradeResult      ModifySLTP(ulong ticket, double sl, double tp);

   // Position queries
   SPositionInfo     GetPosition();
   bool              HasPosition();
   int               PositionCount();

   // Emergency
   bool              FlattenAll();

   // Getters
   ulong             PositionTicket()  { return m_position_ticket; }
   string            Symbol()          { return m_symbol; }
   int               Magic()           { return m_magic; }

private:
   ENUM_ORDER_TYPE_FILLING DetectFillingMode();
   STradeResult      BuildResult(bool success, string error = "");
   void              SyncPosition();
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
COrderManager::COrderManager()
{
   m_symbol = "";
   m_magic = 0;
   m_slippage = 10;
   m_position_ticket = 0;
   m_position_volume = 0;
   m_position_direction = "";
}

//+------------------------------------------------------------------+
//| Initialize with symbol and magic number                           |
//+------------------------------------------------------------------+
bool COrderManager::Init(string symbol, int magic, int slippage_points)
{
   m_symbol = symbol;
   m_magic = magic;
   m_slippage = slippage_points;

   // Verify symbol exists and is tradeable
   if(!SymbolSelect(m_symbol, true))
   {
      PrintFormat("[ORDER] Symbol %s not found or cannot be selected", m_symbol);
      return false;
   }

   // Detect filling mode for this symbol/broker
   m_filling = DetectFillingMode();

   // Configure trade object
   m_trade.SetExpertMagicNumber(m_magic);
   m_trade.SetDeviationInPoints(m_slippage);
   m_trade.SetTypeFilling(m_filling);
   m_trade.SetAsyncMode(false); // Synchronous — we need fill confirmation

   PrintFormat("[ORDER] Initialized: %s magic=%d filling=%d slippage=%d",
               m_symbol, m_magic, m_filling, m_slippage);

   // Sync any existing position
   SyncPosition();

   return true;
}

//+------------------------------------------------------------------+
//| Detect correct filling mode for this symbol                       |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING COrderManager::DetectFillingMode()
{
   long filling_mode = SymbolInfoInteger(m_symbol, SYMBOL_FILLING_MODE);

   // Check in order of preference
   if((filling_mode & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   if((filling_mode & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;

   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
//| Open buy position                                                 |
//+------------------------------------------------------------------+
STradeResult COrderManager::Buy(double volume, double sl, double tp)
{
   double ask = SymbolInfoDouble(m_symbol, SYMBOL_ASK);
   if(ask == 0)
      return BuildResult(false, "Cannot get ASK price");

   // Validate volume
   double min_vol = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_MIN);
   double max_vol = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_MAX);
   double step    = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_STEP);

   volume = MathFloor(volume / step) * step;
   volume = MathMax(volume, min_vol);
   volume = MathMin(volume, max_vol);

   PrintFormat("[ORDER] BUY %.2f %s @ %.2f SL=%.2f TP=%.2f",
               volume, m_symbol, ask, sl, tp);

   if(!m_trade.Buy(volume, m_symbol, ask, sl, tp, "SCALPER"))
   {
      string err = StringFormat("Buy failed: retcode=%d desc=%s",
                                m_trade.ResultRetcode(),
                                m_trade.ResultRetcodeDescription());
      PrintFormat("[ORDER] %s", err);
      return BuildResult(false, err);
   }

   // Success
   STradeResult result;
   result.success = true;
   result.ticket  = m_trade.ResultOrder();
   result.price   = m_trade.ResultPrice();
   result.volume  = m_trade.ResultVolume();
   result.sl      = sl;
   result.tp      = tp;
   result.spread  = (int)SymbolInfoInteger(m_symbol, SYMBOL_SPREAD);
   result.error   = "";

   m_position_ticket = result.ticket;
   m_position_volume = result.volume;
   m_position_direction = "BUY";

   PrintFormat("[ORDER] BUY filled: ticket=%d price=%.2f vol=%.2f",
               result.ticket, result.price, result.volume);

   return result;
}

//+------------------------------------------------------------------+
//| Open sell position                                                |
//+------------------------------------------------------------------+
STradeResult COrderManager::Sell(double volume, double sl, double tp)
{
   double bid = SymbolInfoDouble(m_symbol, SYMBOL_BID);
   if(bid == 0)
      return BuildResult(false, "Cannot get BID price");

   double min_vol = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_MIN);
   double max_vol = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_MAX);
   double step    = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_STEP);

   volume = MathFloor(volume / step) * step;
   volume = MathMax(volume, min_vol);
   volume = MathMin(volume, max_vol);

   PrintFormat("[ORDER] SELL %.2f %s @ %.2f SL=%.2f TP=%.2f",
               volume, m_symbol, bid, sl, tp);

   if(!m_trade.Sell(volume, m_symbol, bid, sl, tp, "SCALPER"))
   {
      string err = StringFormat("Sell failed: retcode=%d desc=%s",
                                m_trade.ResultRetcode(),
                                m_trade.ResultRetcodeDescription());
      PrintFormat("[ORDER] %s", err);
      return BuildResult(false, err);
   }

   STradeResult result;
   result.success = true;
   result.ticket  = m_trade.ResultOrder();
   result.price   = m_trade.ResultPrice();
   result.volume  = m_trade.ResultVolume();
   result.sl      = sl;
   result.tp      = tp;
   result.spread  = (int)SymbolInfoInteger(m_symbol, SYMBOL_SPREAD);
   result.error   = "";

   m_position_ticket = result.ticket;
   m_position_volume = result.volume;
   m_position_direction = "SELL";

   PrintFormat("[ORDER] SELL filled: ticket=%d price=%.2f vol=%.2f",
               result.ticket, result.price, result.volume);

   return result;
}

//+------------------------------------------------------------------+
//| Close specific position                                           |
//+------------------------------------------------------------------+
STradeResult COrderManager::ClosePosition(ulong ticket, string reason)
{
   if(!PositionSelectByTicket(ticket))
      return BuildResult(false, "Position not found: " + IntegerToString(ticket));

   double volume = PositionGetDouble(POSITION_VOLUME);
   double profit = PositionGetDouble(POSITION_PROFIT);
   double commission = PositionGetDouble(POSITION_COMMISSION);
   double swap = PositionGetDouble(POSITION_SWAP);

   PrintFormat("[ORDER] CLOSE ticket=%d vol=%.2f profit=%.2f reason=%s",
               ticket, volume, profit, reason);

   if(!m_trade.PositionClose(ticket, m_slippage))
   {
      string err = StringFormat("Close failed: retcode=%d desc=%s",
                                m_trade.ResultRetcode(),
                                m_trade.ResultRetcodeDescription());
      PrintFormat("[ORDER] %s", err);
      return BuildResult(false, err);
   }

   STradeResult result;
   result.success = true;
   result.ticket  = ticket;
   result.price   = m_trade.ResultPrice();
   result.volume  = volume;
   result.sl      = 0;
   result.tp      = 0;
   result.spread  = (int)SymbolInfoInteger(m_symbol, SYMBOL_SPREAD);
   result.error   = "";

   m_position_ticket = 0;
   m_position_volume = 0;
   m_position_direction = "";

   PrintFormat("[ORDER] CLOSED: ticket=%d price=%.2f pnl=%.2f",
               ticket, result.price, profit + commission + swap);

   return result;
}

//+------------------------------------------------------------------+
//| Modify SL/TP on active position                                   |
//+------------------------------------------------------------------+
STradeResult COrderManager::ModifySLTP(ulong ticket, double sl, double tp)
{
   if(!PositionSelectByTicket(ticket))
      return BuildResult(false, "Position not found for modify");

   if(!m_trade.PositionModify(ticket, sl, tp))
   {
      string err = StringFormat("Modify failed: retcode=%d desc=%s",
                                m_trade.ResultRetcode(),
                                m_trade.ResultRetcodeDescription());
      return BuildResult(false, err);
   }

   STradeResult result;
   result.success = true;
   result.ticket  = ticket;
   result.price   = PositionGetDouble(POSITION_PRICE_OPEN);
   result.volume  = PositionGetDouble(POSITION_VOLUME);
   result.sl      = sl;
   result.tp      = tp;
   result.spread  = (int)SymbolInfoInteger(m_symbol, SYMBOL_SPREAD);
   result.error   = "";

   return result;
}

//+------------------------------------------------------------------+
//| Get current position info                                         |
//+------------------------------------------------------------------+
SPositionInfo COrderManager::GetPosition()
{
   SPositionInfo info;
   info.exists = false;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;

      if(PositionGetString(POSITION_SYMBOL) != m_symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != m_magic) continue;

      info.exists     = true;
      info.ticket     = ticket;
      info.direction  = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      info.volume     = PositionGetDouble(POSITION_VOLUME);
      info.open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      info.sl         = PositionGetDouble(POSITION_SL);
      info.tp         = PositionGetDouble(POSITION_TP);
      info.profit     = PositionGetDouble(POSITION_PROFIT);
      info.commission = PositionGetDouble(POSITION_COMMISSION);
      info.swap       = PositionGetDouble(POSITION_SWAP);
      info.spread     = (int)SymbolInfoInteger(m_symbol, SYMBOL_SPREAD);

      m_position_ticket = ticket;
      m_position_volume = info.volume;
      m_position_direction = info.direction;

      return info;
   }

   // No position found
   m_position_ticket = 0;
   m_position_volume = 0;
   m_position_direction = "";

   return info;
}

//+------------------------------------------------------------------+
//| Check if we have an active position                               |
//+------------------------------------------------------------------+
bool COrderManager::HasPosition()
{
   SPositionInfo info = GetPosition();
   return info.exists;
}

//+------------------------------------------------------------------+
//| Count positions for this symbol/magic                             |
//+------------------------------------------------------------------+
int COrderManager::PositionCount()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != m_symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != m_magic) continue;
      count++;
   }
   return count;
}

//+------------------------------------------------------------------+
//| Emergency: close ALL positions for this symbol/magic              |
//+------------------------------------------------------------------+
bool COrderManager::FlattenAll()
{
   PrintFormat("[ORDER] !!! FLATTEN ALL !!!");
   bool all_closed = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != m_symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != m_magic) continue;

      STradeResult result = ClosePosition(ticket, "FLATTEN");
      if(!result.success)
      {
         PrintFormat("[ORDER] Failed to close ticket %d: %s", ticket, result.error);
         all_closed = false;
      }
   }

   return all_closed;
}

//+------------------------------------------------------------------+
//| Sync position state on startup/reconnect                          |
//+------------------------------------------------------------------+
void COrderManager::SyncPosition()
{
   SPositionInfo info = GetPosition();
   if(info.exists)
   {
      PrintFormat("[ORDER] Existing position found: %s %s %.2f @ %.2f",
                  info.direction, m_symbol, info.volume, info.open_price);
   }
}

//+------------------------------------------------------------------+
//| Build error result                                                |
//+------------------------------------------------------------------+
STradeResult COrderManager::BuildResult(bool success, string error)
{
   STradeResult result;
   result.success = success;
   result.ticket  = 0;
   result.price   = 0;
   result.volume  = 0;
   result.sl      = 0;
   result.tp      = 0;
   result.spread  = 0;
   result.error   = error;
   return result;
}

#endif // ORDER_MANAGER_MQH
