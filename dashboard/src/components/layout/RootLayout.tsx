import { Outlet } from "react-router-dom";
import { Header } from "./Header";

export function RootLayout() {
  return (
    <div className="relative flex min-h-screen flex-col">
      <Header />
      <main className="flex-1">
        <div className="container py-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
