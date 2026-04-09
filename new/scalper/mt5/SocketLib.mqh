//+------------------------------------------------------------------+
//|                                                    SocketLib.mqh |
//|                         TCP Socket wrapper for Python IPC        |
//|             Uses native MQL5 socket functions (build 1755+)      |
//+------------------------------------------------------------------+
#ifndef SOCKET_LIB_MQH
#define SOCKET_LIB_MQH

#include <JAson.mqh>  // JSON parser — standard MQL5 include

//--- Connection states
enum ENUM_SOCKET_STATE
{
   SOCKET_DISCONNECTED = 0,
   SOCKET_CONNECTING   = 1,
   SOCKET_CONNECTED    = 2,
   SOCKET_ERROR        = 3
};

//+------------------------------------------------------------------+
//| Socket wrapper with reconnection and heartbeat                    |
//+------------------------------------------------------------------+
class CSocketLib
{
private:
   int               m_socket;
   string            m_host;
   int               m_port;
   ENUM_SOCKET_STATE m_state;

   // Heartbeat
   datetime          m_last_hb_recv;
   datetime          m_last_hb_sent;
   int               m_hb_interval_ms;
   int               m_hb_timeout_ms;

   // Reconnection
   int               m_reconnect_base_ms;
   int               m_reconnect_max_ms;
   int               m_reconnect_current_ms;
   datetime          m_last_reconnect_attempt;
   int               m_reconnect_attempts;

   // Receive buffer
   string            m_recv_buffer;

public:
                     CSocketLib();
                    ~CSocketLib();

   // Configuration
   void              SetAddress(string host, int port);
   void              SetHeartbeat(int interval_ms, int timeout_ms);
   void              SetReconnect(int base_ms, int max_ms);

   // Connection
   bool              Connect();
   void              Disconnect();
   bool              IsConnected()     { return m_state == SOCKET_CONNECTED; }
   ENUM_SOCKET_STATE State()           { return m_state; }

   // Send
   bool              SendJSON(CJAVal &json);
   bool              SendRaw(string data);

   // Receive (non-blocking)
   bool              HasData();
   bool              ReadMessage(CJAVal &json);

   // Heartbeat
   void              SendHeartbeat();
   bool              CheckHeartbeat();

   // Reconnection
   bool              TryReconnect();
   void              ResetReconnect();

private:
   bool              SendBytes(string data);
   string            ReadLine();
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
CSocketLib::CSocketLib()
{
   m_socket = INVALID_HANDLE;
   m_host = "127.0.0.1";
   m_port = 5555;
   m_state = SOCKET_DISCONNECTED;

   m_last_hb_recv = 0;
   m_last_hb_sent = 0;
   m_hb_interval_ms = 1000;
   m_hb_timeout_ms = 5000;

   m_reconnect_base_ms = 1000;
   m_reconnect_max_ms = 30000;
   m_reconnect_current_ms = 1000;
   m_last_reconnect_attempt = 0;
   m_reconnect_attempts = 0;

   m_recv_buffer = "";
}

//+------------------------------------------------------------------+
//| Destructor                                                        |
//+------------------------------------------------------------------+
CSocketLib::~CSocketLib()
{
   Disconnect();
}

//+------------------------------------------------------------------+
//| Configuration                                                     |
//+------------------------------------------------------------------+
void CSocketLib::SetAddress(string host, int port)
{
   m_host = host;
   m_port = port;
}

void CSocketLib::SetHeartbeat(int interval_ms, int timeout_ms)
{
   m_hb_interval_ms = interval_ms;
   m_hb_timeout_ms = timeout_ms;
}

void CSocketLib::SetReconnect(int base_ms, int max_ms)
{
   m_reconnect_base_ms = base_ms;
   m_reconnect_max_ms = max_ms;
   m_reconnect_current_ms = base_ms;
}

//+------------------------------------------------------------------+
//| Connect to Python server                                          |
//+------------------------------------------------------------------+
bool CSocketLib::Connect()
{
   if(m_state == SOCKET_CONNECTED)
      return true;

   m_socket = SocketCreate();
   if(m_socket == INVALID_HANDLE)
   {
      PrintFormat("[SOCKET] SocketCreate failed: %d", GetLastError());
      m_state = SOCKET_ERROR;
      return false;
   }

   m_state = SOCKET_CONNECTING;

   if(!SocketConnect(m_socket, m_host, m_port, 3000))
   {
      PrintFormat("[SOCKET] Connect failed to %s:%d err=%d", m_host, m_port, GetLastError());
      SocketClose(m_socket);
      m_socket = INVALID_HANDLE;
      m_state = SOCKET_DISCONNECTED;
      return false;
   }

   m_state = SOCKET_CONNECTED;
   m_last_hb_recv = (datetime)TimeLocal();
   m_last_hb_sent = (datetime)TimeLocal();
   m_recv_buffer = "";
   ResetReconnect();

   PrintFormat("[SOCKET] Connected to %s:%d", m_host, m_port);
   return true;
}

//+------------------------------------------------------------------+
//| Disconnect                                                        |
//+------------------------------------------------------------------+
void CSocketLib::Disconnect()
{
   if(m_socket != INVALID_HANDLE)
   {
      SocketClose(m_socket);
      m_socket = INVALID_HANDLE;
   }
   m_state = SOCKET_DISCONNECTED;
   m_recv_buffer = "";
}

//+------------------------------------------------------------------+
//| Send JSON object as newline-delimited string                      |
//+------------------------------------------------------------------+
bool CSocketLib::SendJSON(CJAVal &json)
{
   if(m_state != SOCKET_CONNECTED)
      return false;

   string data = json.Serialize() + "\n";
   return SendBytes(data);
}

//+------------------------------------------------------------------+
//| Send raw string                                                   |
//+------------------------------------------------------------------+
bool CSocketLib::SendRaw(string data)
{
   if(m_state != SOCKET_CONNECTED)
      return false;
   return SendBytes(data + "\n");
}

//+------------------------------------------------------------------+
//| Low-level byte send                                               |
//+------------------------------------------------------------------+
bool CSocketLib::SendBytes(string data)
{
   uchar bytes[];
   int len = StringToCharArray(data, bytes, 0, WHOLE_ARRAY, CP_UTF8) - 1; // exclude null terminator

   if(len <= 0)
      return false;

   int sent = SocketSend(m_socket, bytes, len);
   if(sent < 0)
   {
      PrintFormat("[SOCKET] Send failed: %d", GetLastError());
      Disconnect();
      return false;
   }

   return (sent == len);
}

//+------------------------------------------------------------------+
//| Check if data is available (non-blocking)                         |
//+------------------------------------------------------------------+
bool CSocketLib::HasData()
{
   if(m_state != SOCKET_CONNECTED)
      return false;

   // Check if we already have a complete message in buffer
   if(StringFind(m_recv_buffer, "\n") >= 0)
      return true;

   return SocketIsReadable(m_socket) > 0;
}

//+------------------------------------------------------------------+
//| Read one complete JSON message (non-blocking)                     |
//+------------------------------------------------------------------+
bool CSocketLib::ReadMessage(CJAVal &json)
{
   if(m_state != SOCKET_CONNECTED)
      return false;

   // Try to read more data if available
   uint available = SocketIsReadable(m_socket);
   if(available > 0)
   {
      uchar buf[];
      int read = SocketRead(m_socket, buf, available, 10); // 10ms timeout
      if(read > 0)
      {
         string chunk = CharArrayToString(buf, 0, read, CP_UTF8);
         m_recv_buffer += chunk;
      }
      else if(read < 0)
      {
         PrintFormat("[SOCKET] Read error: %d", GetLastError());
         Disconnect();
         return false;
      }
   }

   // Extract first complete line
   string line = ReadLine();
   if(line == "")
      return false;

   // Parse JSON
   json.Clear();
   if(!json.Deserialize(line))
   {
      PrintFormat("[SOCKET] JSON parse error: %s", StringSubstr(line, 0, 100));
      return false;
   }

   return true;
}

//+------------------------------------------------------------------+
//| Extract first newline-delimited line from buffer                  |
//+------------------------------------------------------------------+
string CSocketLib::ReadLine()
{
   int pos = StringFind(m_recv_buffer, "\n");
   if(pos < 0)
      return "";

   string line = StringSubstr(m_recv_buffer, 0, pos);
   m_recv_buffer = StringSubstr(m_recv_buffer, pos + 1);

   // Trim whitespace
   StringTrimLeft(line);
   StringTrimRight(line);

   return line;
}

//+------------------------------------------------------------------+
//| Send heartbeat                                                    |
//+------------------------------------------------------------------+
void CSocketLib::SendHeartbeat()
{
   if(m_state != SOCKET_CONNECTED)
      return;

   ulong now_ms = GetTickCount64();
   ulong last_ms = (ulong)m_last_hb_sent * 1000;

   if((int)(now_ms - last_ms) < m_hb_interval_ms)
      return;

   CJAVal hb;
   hb["type"] = "heartbeat";
   hb["time"] = (long)(TimeCurrent());
   SendJSON(hb);

   m_last_hb_sent = (datetime)TimeLocal();
}

//+------------------------------------------------------------------+
//| Check if heartbeat has timed out                                  |
//+------------------------------------------------------------------+
bool CSocketLib::CheckHeartbeat()
{
   if(m_state != SOCKET_CONNECTED)
      return false;

   if(m_last_hb_recv == 0)
      return true; // no heartbeat received yet, still ok

   int elapsed_sec = (int)(TimeLocal() - m_last_hb_recv);
   int timeout_sec = m_hb_timeout_ms / 1000;

   if(elapsed_sec > timeout_sec)
   {
      PrintFormat("[SOCKET] Heartbeat timeout: %d sec > %d sec", elapsed_sec, timeout_sec);
      return false; // timeout!
   }

   return true;
}

//+------------------------------------------------------------------+
//| Try reconnect with exponential backoff + jitter                   |
//+------------------------------------------------------------------+
bool CSocketLib::TryReconnect()
{
   if(m_state == SOCKET_CONNECTED)
      return true;

   int elapsed_ms = (int)((TimeLocal() - m_last_reconnect_attempt) * 1000);
   if(elapsed_ms < m_reconnect_current_ms)
      return false; // not time yet

   m_last_reconnect_attempt = (datetime)TimeLocal();
   m_reconnect_attempts++;

   PrintFormat("[SOCKET] Reconnect attempt #%d (backoff: %dms)",
               m_reconnect_attempts, m_reconnect_current_ms);

   if(Connect())
   {
      PrintFormat("[SOCKET] Reconnected after %d attempts", m_reconnect_attempts);
      return true;
   }

   // Exponential backoff with jitter
   m_reconnect_current_ms = MathMin(
      m_reconnect_current_ms * 2 + MathRand() % 1000,
      m_reconnect_max_ms
   );

   return false;
}

//+------------------------------------------------------------------+
//| Reset reconnect state                                             |
//+------------------------------------------------------------------+
void CSocketLib::ResetReconnect()
{
   m_reconnect_current_ms = m_reconnect_base_ms;
   m_reconnect_attempts = 0;
}

#endif // SOCKET_LIB_MQH
