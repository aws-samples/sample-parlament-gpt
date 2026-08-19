module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  testMatch: ["**/test/**/*.test.ts"],
  // cdk.out contains synthesized Docker assets (incl. the frontend's own tests); don't crawl it.
  testPathIgnorePatterns: ["/node_modules/", "/cdk.out/"],
  modulePathIgnorePatterns: ["/cdk.out/"],
};
