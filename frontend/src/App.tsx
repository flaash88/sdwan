import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { useAuth } from "./lib/auth";
import AuditPage from "./pages/Audit";
import Dashboard from "./pages/Dashboard";
import DeviceDetail from "./pages/DeviceDetail";
import Devices from "./pages/Devices";
import Login from "./pages/Login";
import Sites from "./pages/Sites";
import Tenants from "./pages/Tenants";
import Users from "./pages/Users";
import { extraRoutes } from "./routes";

export default function App() {
  const { me, loading } = useAuth();
  if (loading) return <div className="p-10 text-slate-400">Lade …</div>;
  if (!me)
    return (
      <Routes>
        <Route path="*" element={<Login />} />
      </Routes>
    );
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/devices" element={<Devices />} />
        <Route path="/devices/:id" element={<DeviceDetail />} />
        <Route path="/sites" element={<Sites />} />
        <Route path="/tenants" element={<Tenants />} />
        <Route path="/users" element={<Users />} />
        <Route path="/audit" element={<AuditPage />} />
        {extraRoutes.map((r) => (
          <Route key={r.path} path={r.path} element={r.element} />
        ))}
        <Route path="/login" element={<Navigate to="/" />} />
        <Route path="*" element={<div className="text-slate-500">Seite nicht gefunden</div>} />
      </Routes>
    </Layout>
  );
}
