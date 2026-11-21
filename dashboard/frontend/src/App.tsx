import { lazy, Suspense } from "react";
import { Navigate, useLocation } from "react-router-dom";

const V2App = lazy(() => import("./v2/shell/V2App"));

// The dashboard is the new pages only. Every path outside /app opens the home overview.
export default function App() {
  const { pathname } = useLocation();
  if (pathname === "/app" || pathname.startsWith("/app/")) {
    return (
      <Suspense fallback={null}>
        <V2App />
      </Suspense>
    );
  }
  return <Navigate to="/app/home/overview" replace />;
}
