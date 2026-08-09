import type { Metadata } from "next";

import { RegisterForm } from "../../components/auth/RegisterForm";

export const metadata: Metadata = {
  title: "Sign up · SmartReco",
};

export default function RegisterPage(): React.ReactElement {
  return (
    <main className="mx-auto max-w-sm px-6 py-16">
      <h1 className="text-2xl font-bold">Create your account</h1>
      <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
        Browse a bit, and SmartReco starts building a recommendation for you.
      </p>
      <RegisterForm />
    </main>
  );
}
