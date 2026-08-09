import type { Metadata } from "next";

import { RegisterForm } from "../../components/auth/RegisterForm";

export const metadata: Metadata = {
  title: "Sign up · SmartReco",
};

export default function RegisterPage(): React.ReactElement {
  return (
    <main className="mx-auto max-w-sm px-6 py-20">
      <p className="eyebrow">Get started</p>
      <h1 className="mt-2 text-3xl">Create your account</h1>
      <p className="mt-2 text-muted">
        Browse a bit, and SmartReco starts building a recommendation for you.
      </p>
      <RegisterForm />
    </main>
  );
}
