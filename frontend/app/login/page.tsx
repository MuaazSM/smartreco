import type { Metadata } from "next";

import { LoginForm } from "../../components/auth/LoginForm";

export const metadata: Metadata = {
  title: "Log in · SmartReco",
};

export default function LoginPage(): React.ReactElement {
  return (
    <main className="mx-auto max-w-sm px-6 py-20">
      <p className="eyebrow">Welcome back</p>
      <h1 className="mt-2 text-3xl">Log in</h1>
      <p className="mt-2 text-muted">
        See the recommendation built from your activity — cited to real courses.
      </p>
      <LoginForm />
    </main>
  );
}
