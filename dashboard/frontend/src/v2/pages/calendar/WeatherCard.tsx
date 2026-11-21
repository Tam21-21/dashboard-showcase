import { Block, State } from "../../kit";
import { owmKind } from "./accent";
import type { WxKind } from "./accent";
import { serverDetail } from "../../../lib/errorText";
import type { WeatherData, WeatherForecastDay } from "../../../types";
import { addDays } from "./data";

const COLOUR: Record<WxKind, string> = { sun: "#ffb020", part: "#e8c27a", cloud: "#8d98a8", rain: "#4d9bff", snow: "#9fe3f0" };

// One small line icon per condition kind, drawn from the real weather code (owmKind).
export function WxIcon({ icon, size = 18 }: { icon: string | undefined; size?: number }) {
  const kind = owmKind(icon);
  const cloud = "M7 18a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17 9.5 4.3 4.3 0 0 1 17 18z";
  return (
    <svg className="wx" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" style={{ stroke: COLOUR[kind] }}>
      {kind === "sun" && (
        <>
          <circle cx="12" cy="12" r="4.3" />
          <path d="M12 2.5V5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8" />
        </>
      )}
      {kind === "part" && (
        <>
          <circle cx="9" cy="8" r="3.2" />
          <path d="M9 2.5V3.8M3.5 8h1.3M5.1 4.1l.9.9M12.9 4.1l-.9.9" />
          <path d="M9 19a3.5 3.5 0 0 1-.4-7A4.8 4.8 0 0 1 18 12.3 3.4 3.4 0 0 1 17.5 19z" />
        </>
      )}
      {kind === "cloud" && <path d={cloud} />}
      {kind === "rain" && (
        <>
          <path d="M7 15a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17 6.5 4.3 4.3 0 0 1 17 15z" />
          <path d="M8 18l-1 3M12 18l-1 3M16 18l-1 3" />
        </>
      )}
      {kind === "snow" && (
        <>
          <path d="M7 15a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17 6.5 4.3 4.3 0 0 1 17 15z" />
          <path d="M8 18.5v.01M12 20v.01M16 18.5v.01" />
        </>
      )}
    </svg>
  );
}

const capital = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
export const forecastFor = (rows: WeatherForecastDay[] | undefined, day: string) => rows?.find((r) => r.date === day);

interface Q<T> { data: T | undefined; isError: boolean; error: unknown }

export default function WeatherCard({ today, weather, forecast }: { today: string; weather: Q<WeatherData>; forecast: Q<WeatherForecastDay[]> }) {
  // The server answers {} with status 200 when it has no key or the service failed with nothing cached.
  const w = weather.data && typeof weather.data.city === "string" ? weather.data : undefined;
  const whyW = weather.isError
    ? serverDetail(weather.error)
    : weather.data
      ? "The weather service sent no reading"
      : "Loading";
  const tomorrow = forecastFor(forecast.data, addDays(today, 1));
  const whyT = forecast.isError ? serverDetail(forecast.error) : forecast.data ? "No forecast for tomorrow" : "Loading";
  const why = w ? null : whyW;
  return (
    <div data-testid="wx-card" className="cal-wx-w">
      <Block title="Weather" state={w ? undefined : <State tone="dim">Not read</State>}>
        <div className="xwx">
          <div>
            <span title={w ? w.city : undefined}>Today{w && <WxIcon icon={w.icon} size={18} />}</span>
            {w ? (
              <>
                <b className="num">{Math.round(w.temp_max)}°</b>
                <small>{capital(w.description)}, {Math.round(w.temp_min)}° tonight</small>
              </>
            ) : (
              <>
                <b className="num" title={whyW}>-</b>
                <small>High, and the low tonight</small>
              </>
            )}
          </div>
          <div data-testid="wx-tomorrow">
            <span>Tomorrow{tomorrow && <WxIcon icon={tomorrow.icon} size={18} />}</span>
            {tomorrow ? (
              <>
                <b className="num">{Math.round(tomorrow.temp_max)}°</b>
                <small>Low {Math.round(tomorrow.temp_min)}°</small>
              </>
            ) : (
              <>
                <b className="num" title={whyT}>-</b>
                <small>High and low</small>
              </>
            )}
          </div>
        </div>
        {why && <p className="xn xwn">{why}</p>}
      </Block>
    </div>
  );
}
