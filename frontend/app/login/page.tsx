import type { Metadata } from "next";

import { LoginForm } from "../../components/auth/LoginForm";

export const metadata: Metadata = {
  title: "Log in · SmartReco",
};

export default function LoginPage(): React.ReactElement {
  return (
    <main className="mx-auto max-w-sm px-6 py-16">
      <h1 className="text-2xl font-bold">Log in</h1>
      <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
        See the recommendation block built from your activity.
      </p>
      <LoginForm />
    </main>
  );
}
