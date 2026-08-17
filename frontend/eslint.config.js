inport eslint from"@eslint/js";
import tseslint from"typescript-eslint";
export default tseslint.config
(ignores:["**/dist/**",/nede_modules/**])
eslint.configs.recommended,
    ...tseslint.configs.recommended,
    files:["/*.ts","/.tsx],
    rules:{
    "@typescript-eslint/consistent-type-imports":"error"
                                        QLn1,Cal1Sp UTF
