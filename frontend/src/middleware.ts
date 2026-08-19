import { NextRequest, NextResponse } from "next/server";
import { ID_TOKEN_COOKIE, verifyIdToken } from "@/lib/auth";

// Protect every route except the auth endpoints and static assets. The Cognito hosted UI
// handles sign-in/sign-up; there is no app-local login page.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|api/auth).*)"],
};

export async function middleware(req: NextRequest) {
  const user = await verifyIdToken(req.cookies.get(ID_TOKEN_COOKIE)?.value);
  if (user) return NextResponse.next();

  // Unauthenticated API calls get a clean 401 instead of an HTML redirect.
  if (req.nextUrl.pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const loginUrl = new URL("/api/auth/login", req.url);
  loginUrl.searchParams.set("next", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(loginUrl);
}
