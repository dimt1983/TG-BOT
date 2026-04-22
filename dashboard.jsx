import { useState, useEffect } from "react";

const MOCK_DATA = {
  stats: {
    revenue_week: 284500,
    revenue_month: 1120000,
    orders_week: 47,
    orders_month: 189,
    clients_total: 83,
    new_clients_week: 6,
    out_of_stock: 12,
    low_stock: 8,
  },
  top_products: [
    { name: "Бразилия Серрадо 1 кг", qty: 124, revenue: 249800 },
    { name: "КЛАССИКА 1 кг", qty: 89, revenue: 175775 },
    { name: "Кения АА (Drip) 1 шт", qty: 312, revenue: 109200 },
    { name: "Сироп BOTANIKA Карамель 1л", qty: 67, revenue: 0 },
    { name: "Эфиопия Milk 1 кг", qty: 41, revenue: 82615 },
  ],
  low_stock_items: [
    { name: "Кения АБ Центральная провинция 1 кг", stock: 1 },
    { name: "Руанда Мутетели 1 кг", stock: 2 },
    { name: "Эфиопия Ададо 1 кг", stock: 4 },
    { name: "Танзания АА 1 кг", stock: 3 },
    { name: "Эфиопия Чelelекту гр.1 200 г", stock: 4 },
  ],
  recent_orders: [
    { id: 142, name: "ООО Кофейня Мира", total: 18500, date: "22.04", items: "Серрадо 5кг, Классика 3кг" },
    { id: 141, name: "Иванов Д.", total: 4350, date: "22.04", items: "Кения АА Drip x58" },
    { id: 140, name: "ИП Петрова", total: 12800, date: "21.04", items: "BOTANIKA сиропы x12" },
    { id: 139, name: "Сидоров А.", total: 2015, date: "21.04", items: "Бразилия Серрадо 1кг" },
    { id: 138, name: "Кофе Хаус ООО", total: 34200, date: "20.04", items: "Ассорти 18 позиций" },
  ],
  weekly_chart: [
    { day: "Пн", orders: 5, revenue: 28400 },
    { day: "Вт", orders: 8, revenue: 45200 },
    { day: "Ср", orders: 6, revenue: 31800 },
    { day: "Чт", orders: 11, revenue: 67500 },
    { day: "Пт", orders: 9, revenue: 52100 },
    { day: "Сб", orders: 4, revenue: 24300 },
    { day: "Вс", orders: 4, revenue: 35200 },
  ],
};

const formatNum = (n) =>
  n >= 1000000
    ? (n / 1000000).toFixed(1) + "М"
    : n >= 1000
    ? (n / 1000).toFixed(0) + "К"
    : n.toString();

export default function Dashboard() {
  const [data] = useState(MOCK_DATA);
  const [activeTab, setActiveTab] = useState("overview");
  const [time, setTime] = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const maxRevenue = Math.max(...data.weekly_chart.map((d) => d.revenue));

  return (
    <div style={{
      minHeight: "100vh",
      background: "#0a0a0a",
      color: "#e8e0d4",
      fontFamily: "'Georgia', 'Times New Roman', serif",
      padding: "0",
    }}>
      {/* Header */}
      <div style={{
        borderBottom: "1px solid #2a2520",
        padding: "20px 32px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        background: "#0d0c0b",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{
            width: 36, height: 36,
            background: "linear-gradient(135deg, #c8a96e, #8b6914)",
            borderRadius: "50%",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 18,
          }}>☕</div>
          <div>
            <div style={{ fontSize: 18, fontWeight: "bold", letterSpacing: "0.05em", color: "#c8a96e" }}>
              ROASTBERRY
            </div>
            <div style={{ fontSize: 11, color: "#6b6055", letterSpacing: "0.15em", textTransform: "uppercase" }}>
              Agent Dashboard
            </div>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 22, color: "#c8a96e", fontVariantNumeric: "tabular-nums" }}>
            {time.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </div>
          <div style={{ fontSize: 11, color: "#6b6055" }}>
            {time.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" })}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div style={{
        display: "flex", gap: 0,
        borderBottom: "1px solid #2a2520",
        padding: "0 32px",
        background: "#0d0c0b",
      }}>
        {[
          { id: "overview", label: "Обзор" },
          { id: "products", label: "Товары" },
          { id: "orders", label: "Заказы" },
          { id: "alerts", label: `Алерты ${data.stats.low_stock > 0 ? `(${data.stats.low_stock})` : ""}` },
        ].map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            style={{
              background: "none", border: "none",
              padding: "14px 20px",
              fontSize: 13,
              letterSpacing: "0.08em",
              cursor: "pointer",
              color: activeTab === tab.id ? "#c8a96e" : "#6b6055",
              borderBottom: activeTab === tab.id ? "2px solid #c8a96e" : "2px solid transparent",
              marginBottom: -1,
              transition: "color 0.2s",
              fontFamily: "inherit",
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div style={{ padding: "28px 32px", maxWidth: 1200 }}>

        {/* OVERVIEW TAB */}
        {activeTab === "overview" && (
          <div>
            {/* KPI cards */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16, marginBottom: 28 }}>
              {[
                { label: "Выручка за неделю", value: formatNum(data.stats.revenue_week) + " ₽", sub: `+${data.stats.orders_week} заказов`, color: "#c8a96e" },
                { label: "За месяц", value: formatNum(data.stats.revenue_month) + " ₽", sub: `${data.stats.orders_month} заказов`, color: "#8fba8f" },
                { label: "Клиентов всего", value: data.stats.clients_total, sub: `+${data.stats.new_clients_week} за неделю`, color: "#8bb5d4" },
                { label: "Проблем со складом", value: data.stats.out_of_stock + data.stats.low_stock, sub: `${data.stats.out_of_stock} нет / ${data.stats.low_stock} мало`, color: "#d4887a" },
              ].map((kpi) => (
                <div key={kpi.label} style={{
                  background: "#111009",
                  border: "1px solid #2a2520",
                  borderRadius: 8,
                  padding: "20px 22px",
                }}>
                  <div style={{ fontSize: 11, color: "#6b6055", letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 8 }}>
                    {kpi.label}
                  </div>
                  <div style={{ fontSize: 28, fontWeight: "bold", color: kpi.color, lineHeight: 1 }}>
                    {kpi.value}
                  </div>
                  <div style={{ fontSize: 12, color: "#4a4540", marginTop: 6 }}>
                    {kpi.sub}
                  </div>
                </div>
              ))}
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
              {/* Weekly chart */}
              <div style={{
                background: "#111009", border: "1px solid #2a2520",
                borderRadius: 8, padding: "20px 22px",
              }}>
                <div style={{ fontSize: 12, color: "#6b6055", letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 20 }}>
                  Выручка по дням
                </div>
                <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 120 }}>
                  {data.weekly_chart.map((d) => (
                    <div key={d.day} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 6 }}>
                      <div style={{ fontSize: 10, color: "#6b6055" }}>{formatNum(d.revenue)}</div>
                      <div style={{
                        width: "100%",
                        height: Math.round((d.revenue / maxRevenue) * 80),
                        background: "linear-gradient(to top, #8b6914, #c8a96e)",
                        borderRadius: "3px 3px 0 0",
                        minHeight: 4,
                        transition: "height 0.3s",
                      }} />
                      <div style={{ fontSize: 11, color: "#4a4540" }}>{d.day}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Top products */}
              <div style={{
                background: "#111009", border: "1px solid #2a2520",
                borderRadius: 8, padding: "20px 22px",
              }}>
                <div style={{ fontSize: 12, color: "#6b6055", letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 16 }}>
                  Топ продаж
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  {data.top_products.map((p, i) => {
                    const maxQty = data.top_products[0].qty;
                    return (
                      <div key={p.name}>
                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                          <span style={{ fontSize: 12, color: "#c4b89a" }}>{i + 1}. {p.name.replace(" 1 кг", "").replace(" 1 шт", "").slice(0, 30)}</span>
                          <span style={{ fontSize: 12, color: "#c8a96e" }}>{p.qty} шт</span>
                        </div>
                        <div style={{ height: 3, background: "#1e1c18", borderRadius: 2 }}>
                          <div style={{
                            height: "100%",
                            width: `${(p.qty / maxQty) * 100}%`,
                            background: `hsl(${35 + i * 15}, 55%, ${50 - i * 5}%)`,
                            borderRadius: 2,
                          }} />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* PRODUCTS TAB */}
        {activeTab === "products" && (
          <div>
            <div style={{
              background: "#111009", border: "1px solid #2a2520",
              borderRadius: 8, padding: "20px 22px",
            }}>
              <div style={{ fontSize: 12, color: "#6b6055", letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 16 }}>
                Критически низкий остаток
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {data.low_stock_items.map((p) => (
                  <div key={p.name} style={{
                    display: "flex", justifyContent: "space-between",
                    padding: "10px 14px",
                    borderRadius: 6,
                    background: p.stock <= 2 ? "#1a0f0f" : "#14120e",
                    border: `1px solid ${p.stock <= 2 ? "#3d1515" : "#2a2520"}`,
                  }}>
                    <span style={{ fontSize: 13, color: "#c4b89a" }}>{p.name}</span>
                    <span style={{
                      fontSize: 13, fontWeight: "bold",
                      color: p.stock <= 2 ? "#d4887a" : "#e0b96a",
                      minWidth: 40, textAlign: "right",
                    }}>
                      {p.stock} шт
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ORDERS TAB */}
        {activeTab === "orders" && (
          <div style={{
            background: "#111009", border: "1px solid #2a2520",
            borderRadius: 8, padding: "20px 22px",
          }}>
            <div style={{ fontSize: 12, color: "#6b6055", letterSpacing: "0.1em", textTransform: "uppercase", marginBottom: 16 }}>
              Последние заказы
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {data.recent_orders.map((o) => (
                <div key={o.id} style={{
                  display: "grid",
                  gridTemplateColumns: "50px 1fr 1fr auto",
                  gap: 12,
                  padding: "12px 14px",
                  borderRadius: 6,
                  background: "#14120e",
                  border: "1px solid #2a2520",
                  alignItems: "center",
                }}>
                  <span style={{ fontSize: 12, color: "#4a4540" }}>#{o.id}</span>
                  <span style={{ fontSize: 13, color: "#c4b89a" }}>{o.name}</span>
                  <span style={{ fontSize: 12, color: "#6b6055" }}>{o.items.slice(0, 35)}</span>
                  <div style={{ textAlign: "right" }}>
                    <div style={{ fontSize: 14, color: "#c8a96e", fontWeight: "bold" }}>{o.total.toLocaleString()} ₽</div>
                    <div style={{ fontSize: 11, color: "#4a4540" }}>{o.date}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ALERTS TAB */}
        {activeTab === "alerts" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {data.low_stock_items.map((p) => (
              <div key={p.name} style={{
                display: "flex", alignItems: "center", gap: 14,
                padding: "14px 18px",
                background: "#111009",
                border: `1px solid ${p.stock <= 2 ? "#3d2020" : "#2a2520"}`,
                borderRadius: 8,
              }}>
                <div style={{
                  width: 8, height: 8, borderRadius: "50%",
                  background: p.stock <= 2 ? "#d4887a" : "#e0b96a",
                  flexShrink: 0,
                  boxShadow: `0 0 8px ${p.stock <= 2 ? "#d4887a80" : "#e0b96a80"}`,
                }} />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, color: "#c4b89a" }}>{p.name}</div>
                  <div style={{ fontSize: 11, color: "#6b6055", marginTop: 2 }}>
                    Остаток: {p.stock} шт
                  </div>
                </div>
                <div style={{
                  fontSize: 11, padding: "3px 10px",
                  borderRadius: 12,
                  background: p.stock <= 2 ? "#2a1010" : "#1e1a0e",
                  color: p.stock <= 2 ? "#d4887a" : "#e0b96a",
                  border: `1px solid ${p.stock <= 2 ? "#3d2020" : "#2e2810"}`,
                }}>
                  {p.stock <= 2 ? "КРИТИЧНО" : "МАЛО"}
                </div>
              </div>
            ))}
            {data.stats.out_of_stock > 0 && (
              <div style={{
                padding: "14px 18px",
                background: "#111009",
                border: "1px solid #3d1515",
                borderRadius: 8,
                color: "#d4887a",
                fontSize: 13,
              }}>
                ❌ {data.stats.out_of_stock} товаров полностью отсутствуют на складе
              </div>
            )}
          </div>
        )}

        {/* Footer */}
        <div style={{
          marginTop: 32,
          paddingTop: 16,
          borderTop: "1px solid #1e1c18",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}>
          <div style={{ fontSize: 11, color: "#3a3530", letterSpacing: "0.1em" }}>
            ROASTBERRY AGENT v1.0 — данные обновляются через Telegram бота
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            {["🟢 Бот онлайн", "🟢 БД подключена", "🟢 Агент активен"].map((s) => (
              <span key={s} style={{
                fontSize: 10, padding: "2px 8px",
                background: "#111009",
                border: "1px solid #2a2520",
                borderRadius: 10,
                color: "#4a7a4a",
              }}>{s}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
